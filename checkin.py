#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from utils.config import AccountConfig, AppConfig, load_accounts_config, load_raw_accounts_data
from utils.cookie_refresher import refresh_failed_accounts
from utils.notify import notify

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'


class ClientManager:
	"""管理域名级别的 httpx.Client 实例

	确保每个不同的域名都有独立的客户端连接，避免跨域 Cookie 污染
	和 HTTP/2 连接复用问题
	"""

	def __init__(self):
		"""初始化客户端管理器"""
		self.clients: dict[str, httpx.Client] = {}

	def get_client(self, domain: str) -> httpx.Client:
		"""获取指定域名的客户端

		Args:
			domain: 完整的域名（包括 http:// 或 https://）

		Returns:
			该域名的 httpx.Client 实例
		"""
		if domain not in self.clients:
			self.clients[domain] = httpx.Client(http2=True, timeout=30.0)
		return self.clients[domain]

	def set_cookies(self, domain: str, cookies: dict) -> None:
		"""为指定域名的客户端设置 cookies

		Args:
			domain: 完整的域名
			cookies: Cookie 字典
		"""
		client = self.get_client(domain)
		client.cookies.update(cookies)

	def close_all(self) -> None:
		"""关闭所有管理的客户端连接"""
		for domain, client in self.clients.items():
			try:
				client.close()
				print(f'[CLEANUP] Closed client for domain: {domain}')
			except Exception as e:
				print(f'[WARNING] Failed to close client for {domain}: {e}')
		self.clients.clear()

	def close_domain(self, domain: str) -> None:
		"""关闭特定域名的客户端

		Args:
			domain: 完整的域名
		"""
		if domain in self.clients:
			try:
				self.clients[domain].close()
				del self.clients[domain]
				print(f'[CLEANUP] Closed client for domain: {domain}')
			except Exception as e:
				print(f'[WARNING] Failed to close client for {domain}: {e}')

	def __enter__(self):
		"""上下文管理器入口"""
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		"""上下文管理器出口，自动关闭所有客户端"""
		self.close_all()
		return False


def load_balance_hash():
	"""加载余额hash"""
	try:
		if os.path.exists(BALANCE_HASH_FILE):
			with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
				return f.read().strip()
	except Exception:  # nosec B110
		pass
	return None


def save_balance_hash(balance_hash):
	"""保存余额hash"""
	try:
		with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
			f.write(balance_hash)
	except Exception as e:
		print(f'Warning: Failed to save balance hash: {e}')


def generate_balance_hash(balances):
	"""生成余额数据的hash"""
	# 将包含 quota 和 used 的结构转换为简单的 quota 值用于 hash 计算
	simple_balances = {k: v['quota'] for k, v in balances.items()} if balances else {}
	balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def parse_cookies(cookies_data):
	"""解析 cookies 数据"""
	if isinstance(cookies_data, dict):
		return cookies_data

	if isinstance(cookies_data, str):
		cookies_dict = {}
		for cookie in cookies_data.split(';'):
			if '=' in cookie:
				key, value = cookie.strip().split('=', 1)
				cookies_dict[key] = value
		return cookies_dict
	return {}


async def get_waf_cookies_with_playwright(account_name: str, login_url: str, required_cookies: list[str]):
	"""使用 Playwright 获取 WAF cookies（隐私模式）"""
	print(f'[PROCESSING] {account_name}: Starting browser to get WAF cookies...')

	async with async_playwright() as p:
		import tempfile

		with tempfile.TemporaryDirectory() as temp_dir:
			context = await p.chromium.launch_persistent_context(
				user_data_dir=temp_dir,
				headless=True,
				user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
				viewport={'width': 1920, 'height': 1080},
				args=[
					'--disable-blink-features=AutomationControlled',
					'--disable-dev-shm-usage',
					'--disable-web-security',
					'--disable-features=VizDisplayCompositor',
					'--no-sandbox',
				],
			)

			page = await context.new_page()

			try:
				print(f'[PROCESSING] {account_name}: Access login page to get initial cookies...')

				await page.goto(login_url, wait_until='networkidle')

				try:
					await page.wait_for_function('document.readyState === "complete"', timeout=5000)
				except Exception:
					await page.wait_for_timeout(3000)

				cookies = await page.context.cookies()

				waf_cookies = {}
				for cookie in cookies:
					cookie_name = cookie.get('name')
					cookie_value = cookie.get('value')
					if cookie_name in required_cookies and cookie_value is not None:
						waf_cookies[cookie_name] = cookie_value

				print(f'[INFO] {account_name}: Got {len(waf_cookies)} WAF cookies')

				missing_cookies = [c for c in required_cookies if c not in waf_cookies]

				if missing_cookies:
					print(f'[FAILED] {account_name}: Missing WAF cookies: {missing_cookies}')
					await context.close()
					return None

				print(f'[SUCCESS] {account_name}: Successfully got all WAF cookies')

				await context.close()

				return waf_cookies

			except Exception as e:
				print(f'[FAILED] {account_name}: Error occurred while getting WAF cookies: {e}')
				await context.close()
				return None


def get_user_info(client_manager: ClientManager, domain: str, headers, user_info_url: str):
	"""获取用户信息

	Args:
		client_manager: ClientManager 实例
		domain: 提供商域名
		headers: 请求头
		user_info_url: 用户信息 URL

	Returns:
		包含用户信息的字典
	"""
	try:
		client = client_manager.get_client(domain)
		response = client.get(user_info_url, headers=headers, timeout=30)
		if response.status_code == 200:
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				quota = round(user_data.get('quota', 0) / 500000, 2)
				used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
				return {
					'success': True,
					'quota': quota,
					'used_quota': used_quota,
					'display': f':money: Current balance: ${quota}, Used: ${used_quota}',
				}
		return {'success': False, 'error': f'Failed to get user info: HTTP {response.status_code}'}
	except Exception as e:
		return {'success': False, 'error': f'Failed to get user info: {str(e)[:50]}...'}


async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
	"""准备请求所需的 cookies（可能包含 WAF cookies）"""
	waf_cookies = {}

	if provider_config.needs_waf_cookies():
		login_url = f'{provider_config.domain}{provider_config.login_path}'
		waf_cookies = await get_waf_cookies_with_playwright(account_name, login_url, provider_config.waf_cookie_names)
		if not waf_cookies:
			print(f'[FAILED] {account_name}: Unable to get WAF cookies')
			return None
	else:
		print(f'[INFO] {account_name}: Bypass WAF not required, using user cookies directly')

	return {**waf_cookies, **user_cookies}


def execute_check_in(client_manager: ClientManager, domain: str, account_name: str, provider_config, headers: dict):
	"""执行签到请求

	Args:
		client_manager: ClientManager 实例
		domain: 提供商域名
		account_name: 账号名称
		provider_config: 提供商配置
		headers: 请求头

	Returns:
		签到是否成功
	"""
	print(f'[NETWORK] {account_name}: Executing check-in')

	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
	client = client_manager.get_client(domain)
	response = client.post(sign_in_url, headers=checkin_headers, timeout=30)

	print(f'[RESPONSE] {account_name}: Response status code {response.status_code}')

	if response.status_code == 200:
		try:
			result = response.json()
			if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
				print(f'[SUCCESS] {account_name}: Check-in successful!')
				return True
			else:
				error_msg = result.get('msg', result.get('message', 'Unknown error'))
				# 检查是否是"已经签到过"的情况，这种情况也算成功
				already_checked_keywords = ['已经签到', '已签到', '重复签到', 'already checked', 'already signed']
				if any(keyword in error_msg.lower() for keyword in already_checked_keywords):
					print(f'[SUCCESS] {account_name}: Already checked in today')
					return True
				print(f'[FAILED] {account_name}: Check-in failed - {error_msg}')
				return False
		except json.JSONDecodeError:
			# 如果不是 JSON 响应，检查是否包含成功标识
			if 'success' in response.text.lower():
				print(f'[SUCCESS] {account_name}: Check-in successful!')
				return True
			else:
				print(f'[FAILED] {account_name}: Check-in failed - Invalid response format')
				return False
	else:
		print(f'[FAILED] {account_name}: Check-in failed - HTTP {response.status_code}')
		return False


def format_check_in_notification(detail: dict) -> str:
	"""格式化签到通知消息

	Args:
		detail: 包含签到详情的字典

	Returns:
		格式化后的通知消息
	"""
	lines = [
		f'[CHECK-IN] {detail["name"]}',
		'  ━━━━━━━━━━━━━━━━━━━━',
		'  📍 签到前',
		f'     💵 余额: ${detail["before_quota"]:.2f}  |  📊 累计消耗: ${detail["before_used"]:.2f}',
		'  📍 签到后',
		f'     💵 余额: ${detail["after_quota"]:.2f}  |  📊 累计消耗: ${detail["after_used"]:.2f}',
	]

	# 判断是否有变化
	has_reward = detail['check_in_reward'] != 0
	has_usage = detail['usage_increase'] != 0

	if has_reward or has_usage:
		lines.append('  ━━━━━━━━━━━━━━━━━━━━')

		# 已签到但期间有使用
		if not has_reward and has_usage:
			lines.append('  ℹ️  今日已签到（期间有使用）')

		# 签到获得
		if has_reward:
			lines.append(f'  🎁 签到获得: +${detail["check_in_reward"]:.2f}')

		# 期间消耗
		if has_usage:
			lines.append(f'  📉 期间消耗: ${detail["usage_increase"]:.2f}')

		# 余额变化
		if detail['balance_change'] != 0:
			change_symbol = '+' if detail['balance_change'] > 0 else ''
			change_emoji = '📈' if detail['balance_change'] > 0 else '📉'
			lines.append(f'  {change_emoji} 余额变化: {change_symbol}${detail["balance_change"]:.2f}')
	else:
		# 无任何变化
		lines.extend(['  ━━━━━━━━━━━━━━━━━━━━', '  ℹ️  今日已签到，无变化'])

	return '\n'.join(lines)


async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig, client_manager: ClientManager):
	"""为单个账号执行签到操作

	Args:
		account: 账号配置
		account_index: 账号索引
		app_config: 应用配置
		client_manager: 客户端管理器

	Returns:
		(success, user_info_before, user_info_after) 元组
	"""
	account_name = account.get_display_name(account_index)
	print(f'\n[PROCESSING] Starting to process {account_name}')

	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		print(f'[FAILED] {account_name}: Provider "{account.provider}" not found in configuration')
		return False, None, None

	print(f'[INFO] {account_name}: Using provider "{account.provider}" ({provider_config.domain})')

	user_cookies = parse_cookies(account.cookies)
	if not user_cookies:
		print(f'[FAILED] {account_name}: Invalid configuration format')
		return False, None, None

	all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
	if not all_cookies:
		return False, None, None

	# 为该域名的客户端设置 cookies
	client_manager.set_cookies(provider_config.domain, all_cookies)

	try:
		headers = {
			'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
			'Accept': 'application/json, text/plain, */*',
			'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
			'Accept-Encoding': 'gzip, deflate, br, zstd',
			'Referer': provider_config.domain,
			'Origin': provider_config.domain,
			'Connection': 'keep-alive',
			'Sec-Fetch-Dest': 'empty',
			'Sec-Fetch-Mode': 'cors',
			'Sec-Fetch-Site': 'same-origin',
			provider_config.api_user_key: account.api_user,
		}

		user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
		user_info_before = get_user_info(client_manager, provider_config.domain, headers, user_info_url)
		if user_info_before and user_info_before.get('success'):
			print(user_info_before['display'])
		elif user_info_before:
			print(user_info_before.get('error', 'Unknown error'))

		if provider_config.needs_manual_check_in():
			success = execute_check_in(client_manager, provider_config.domain, account_name, provider_config, headers)
			# 签到后再次获取用户信息，用于计算签到收益
			user_info_after = get_user_info(client_manager, provider_config.domain, headers, user_info_url)
			return success, user_info_before, user_info_after
		else:
			print(f'[INFO] {account_name}: Check-in completed automatically (triggered by user info request)')
			# 自动签到的情况，再次获取用户信息
			user_info_after = get_user_info(client_manager, provider_config.domain, headers, user_info_url)
			return True, user_info_before, user_info_after

	except Exception as e:
		print(f'[FAILED] {account_name}: Error occurred during check-in process - {str(e)[:50]}...')
		return False, None, None


def parse_args():
	"""解析命令行参数"""
	parser = argparse.ArgumentParser(description='AnyRouter.top 多账号自动签到脚本')
	parser.add_argument(
		'-y', '--yes',
		action='store_true',
		default=False,
		help='签到失败时自动同意打开浏览器刷新 Cookie（跳过确认提示，仍需手动登录）',
	)
	parser.add_argument(
		'--no-refresh',
		action='store_true',
		default=False,
		help='禁用 Cookie 自动刷新（即使签到失败也不尝试刷新）',
	)
	parser.add_argument(
		'--env-file',
		type=str,
		default='.env',
		help='指定 .env 文件路径（默认: .env）',
	)
	return parser.parse_args()


async def run_check_in_round(
	accounts: list[AccountConfig],
	app_config: AppConfig,
	round_label: str = '',
) -> tuple[int, list[dict], list[str], dict, dict]:
	"""执行一轮签到

	Args:
		accounts: 账号配置列表
		app_config: 应用配置
		round_label: 轮次标签（用于日志）

	Returns:
		(success_count, failed_accounts, notification_content, current_balances, account_check_in_details)
	"""
	prefix = f'[{round_label}] ' if round_label else ''
	success_count = 0
	failed_accounts = []  # 签到失败的账号信息
	notification_content = []
	current_balances = {}
	account_check_in_details = {}

	client_manager = ClientManager()

	try:
		for i, account in enumerate(accounts):
			account_key = f'account_{i + 1}'
			try:
				success, user_info_before, user_info_after = await check_in_account(
					account, i, app_config, client_manager
				)
				if success:
					success_count += 1

				should_notify_this_account = False

				if not success:
					should_notify_this_account = True
					account_name = account.get_display_name(i)
					print(f'{prefix}[NOTIFY] {account_name} failed, will send notification')

					# 收集失败账号信息，用于后续 Cookie 刷新
					provider_config = app_config.get_provider(account.provider)
					if provider_config:
						failed_accounts.append({
							'index': i,
							'provider': account.provider,
							'api_user': account.api_user,
							'name': account_name,
							'domain': provider_config.domain,
							'login_path': provider_config.login_path,
						})

				# 存储签到前后的余额信息
				if user_info_after and user_info_after.get('success'):
					current_quota = user_info_after['quota']
					current_used = user_info_after['used_quota']
					current_balances[account_key] = {'quota': current_quota, 'used': current_used}

					# 计算签到收益
					if user_info_before and user_info_before.get('success'):
						before_quota = user_info_before['quota']
						before_used = user_info_before['used_quota']
						after_quota = user_info_after['quota']
						after_used = user_info_after['used_quota']

						# 计算总额度（余额 + 历史消耗）
						total_before = before_quota + before_used
						total_after = after_quota + after_used

						# 签到获得的额度 = 总额度增加量
						check_in_reward = total_after - total_before

						# 本次消耗 = 历史消耗增加量
						usage_increase = after_used - before_used

						# 余额变化
						balance_change = after_quota - before_quota

						account_check_in_details[account_key] = {
							'name': account.get_display_name(i),
							'before_quota': before_quota,
							'before_used': before_used,
							'after_quota': after_quota,
							'after_used': after_used,
							'check_in_reward': check_in_reward,  # 签到获得
							'usage_increase': usage_increase,  # 本次消耗
							'balance_change': balance_change,  # 余额变化
							'success': success,
						}

				if should_notify_this_account:
					account_name = account.get_display_name(i)
					status = '[SUCCESS]' if success else '[FAIL]'
					account_result = f'{status} {account_name}'
					if user_info_after and user_info_after.get('success'):
						account_result += f'\n{user_info_after["display"]}'
					elif user_info_after:
						account_result += f'\n{user_info_after.get("error", "Unknown error")}'
					notification_content.append(account_result)

			except Exception as e:
				account_name = account.get_display_name(i)
				print(f'{prefix}[FAILED] {account_name} processing exception: {e}')
				notification_content.append(f'[FAIL] {account_name} exception: {str(e)[:50]}...')
				# 异常也收集为失败账号
				provider_config = app_config.get_provider(account.provider)
				if provider_config:
					failed_accounts.append({
						'index': i,
						'provider': account.provider,
						'api_user': account.api_user,
						'name': account.get_display_name(i),
						'domain': provider_config.domain,
						'login_path': provider_config.login_path,
					})

	finally:
		client_manager.close_all()
		print(f'{prefix}[SYSTEM] All client connections closed')

	return success_count, failed_accounts, notification_content, current_balances, account_check_in_details


async def main():
	"""主函数"""
	args = parse_args()

	print('[SYSTEM] AnyRouter.top multi-account auto check-in script started (using Playwright)')
	print(f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
	if args.yes:
		print('[INFO] Auto-yes mode enabled (-y): will open browser for cookie refresh on failure')

	app_config = AppConfig.load_from_env()
	print(f'[INFO] Loaded {len(app_config.providers)} provider configuration(s)')

	accounts = load_accounts_config()
	if not accounts:
		print('[FAILED] Unable to load account configuration, program exits')
		sys.exit(1)

	print(f'[INFO] Found {len(accounts)} account configurations')

	last_balance_hash = load_balance_hash()
	total_count = len(accounts)
	need_notify = False
	balance_changed = False

	# ====== 第一轮签到 ======
	success_count, failed_accounts, notification_content, current_balances, account_check_in_details = (
		await run_check_in_round(accounts, app_config, round_label='Round 1')
	)

	if failed_accounts:
		need_notify = True

	# ====== Cookie 刷新逻辑 ======
	if failed_accounts and not args.no_refresh:
		print(f'\n[INFO] {len(failed_accounts)} account(s) failed, attempting cookie refresh...')

		# 加载原始账号数据用于更新
		raw_accounts_data = load_raw_accounts_data()
		if raw_accounts_data:
			refreshed = await refresh_failed_accounts(
				failed_accounts=failed_accounts,
				accounts_data=raw_accounts_data,
				providers_config=app_config.providers,
				auto_yes=args.yes,
				env_file=args.env_file,
			)

			# 如果有刷新成功的，重新加载配置并对这些账号重试签到
			if refreshed > 0:
				print(f'\n[INFO] {refreshed} cookie(s) refreshed, re-running check-in for refreshed accounts...')

				# 重新加载 .env 以获取更新后的 cookies
				load_dotenv(override=True)
				refreshed_accounts = load_accounts_config()

				if refreshed_accounts:
					# 只重试之前失败的账号
					failed_indices = {acc['index'] for acc in failed_accounts}
					retry_accounts = [
						acc for i, acc in enumerate(refreshed_accounts) if i in failed_indices
					]

					# 构建一个只包含重试账号的列表（保持原始索引映射）
					retry_index_map = {}  # retry列表索引 -> 原始索引
					for retry_i, orig_i in enumerate(sorted(failed_indices)):
						if orig_i < len(refreshed_accounts):
							retry_index_map[retry_i] = orig_i

					retry_success, _, retry_notifications, retry_balances, retry_details = (
						await run_check_in_round(retry_accounts, app_config, round_label='Retry')
					)

					# 合并重试结果
					success_count += retry_success

					# 更新余额信息（用重试后的覆盖）
					for retry_key, orig_i in retry_index_map.items():
						orig_key = f'account_{orig_i + 1}'
						retry_key_str = f'account_{retry_key + 1}'
						if retry_key_str in retry_balances:
							current_balances[orig_key] = retry_balances[retry_key_str]
						if retry_key_str in retry_details:
							account_check_in_details[orig_key] = retry_details[retry_key_str]

					# 更新通知内容：移除已重试成功的失败通知
					if retry_success > 0:
						# 从 notification_content 中移除重试成功的账号
						retried_names = {acc.get_display_name(retry_index_map.get(ri, ri))
							for ri, acc in enumerate(retry_accounts)}
						notification_content = [
							n for n in notification_content
							if not any(name in n for name in retried_names)
						]
						# 添加重试的通知
						for n in retry_notifications:
							notification_content.append(f'[RETRY] {n}')

					print(f'[INFO] Retry complete: {retry_success}/{len(retry_accounts)} succeeded')
		else:
			print('[WARNING] Could not load raw accounts data for cookie refresh')

	# 检查余额变化
	current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
	if current_balance_hash:
		if last_balance_hash is None:
			# 首次运行
			balance_changed = True
			need_notify = True
			print('[NOTIFY] First run detected, will send notification with current balances')
		elif current_balance_hash != last_balance_hash:
			# 余额有变化
			balance_changed = True
			need_notify = True
			print('[NOTIFY] Balance changes detected, will send notification')
		else:
			print('[INFO] No balance changes detected')

	# 为有余额变化的情况添加所有成功账号到通知内容
	if balance_changed:
		for i, account in enumerate(accounts):
			account_key = f'account_{i + 1}'
			if account_key in account_check_in_details:
				detail = account_check_in_details[account_key]
				account_name = detail['name']

				# 使用格式化函数生成通知消息
				account_result = format_check_in_notification(detail)

				# 检查是否已经在通知内容中（避免重复）
				if not any(account_name in item for item in notification_content):
					notification_content.append(account_result)

	# 保存当前余额hash
	if current_balance_hash:
		save_balance_hash(current_balance_hash)

	if need_notify and notification_content:
		# 构建通知内容
		summary = [
			'[STATS] Check-in result statistics:',
			f'[SUCCESS] Success: {success_count}/{total_count}',
			f'[FAIL] Failed: {total_count - success_count}/{total_count}',
		]

		if success_count == total_count:
			summary.append('[SUCCESS] All accounts check-in successful!')
		elif success_count > 0:
			summary.append('[WARN] Some accounts check-in successful')
		else:
			summary.append('[ERROR] All accounts check-in failed')

		time_info = f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'

		notify_content = '\n\n'.join([time_info, '\n'.join(notification_content), '\n'.join(summary)])

		print(notify_content)
		notify.push_message('AnyRouter Check-in Alert', notify_content, msg_type='text')
		print('[NOTIFY] Notification sent due to failures or balance changes')
	else:
		print('[INFO] All accounts successful and no balance changes detected, notification skipped')

	# 设置退出码
	sys.exit(0 if success_count > 0 else 1)


def run_main():
	"""运行主函数的包装函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print('\n[WARNING] Program interrupted by user')
		sys.exit(1)
	except Exception as e:
		print(f'\n[FAILED] Error occurred during program execution: {e}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
