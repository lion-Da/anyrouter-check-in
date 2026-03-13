#!/usr/bin/env python3
"""
Cookie 刷新模块

核心设计：
1. 使用 Playwright 持久化用户数据目录（~/.anyrouter-browser-data），
   LinuxDo 的登录态跨次运行保留，用户只需首次登录一次。
2. 所有失败站点共享同一个浏览器实例（同一个 BrowserContext），
   浏览器按域名天然隔离 cookie，LinuxDo OAuth 登录态自动复用。
3. 导航到站点登录页 → 点击 LinuxDo OAuth → 自动回调 → 轮询目标站 session cookie。
"""

import json
import os

from playwright.async_api import async_playwright

# 持久化浏览器用户数据目录（保存 LinuxDo 登录态等）
BROWSER_DATA_DIR = os.path.join(os.path.expanduser('~'), '.anyrouter-browser-data')

# 单个站点 OAuth 回调等待时间（秒）—— 如果 LinuxDo 已登录，回调通常 5~15s 内完成
OAUTH_AUTO_TIMEOUT = 30
# 需要用户手动登录 LinuxDo 时的最大等待时间（秒）
MANUAL_LOGIN_TIMEOUT = 300
# 轮询 cookie 的间隔（秒）
POLL_INTERVAL = 1.5


def update_env_accounts(accounts_data: list, env_file: str = '.env') -> bool:
	"""更新 .env 文件中的 ANYROUTER_ACCOUNTS

	Args:
		accounts_data: 新的账号数据列表
		env_file: .env 文件路径

	Returns:
		是否更新成功
	"""
	try:
		with open(env_file, 'r', encoding='utf-8') as f:
			content = f.read()

		new_accounts_json = json.dumps(accounts_data, ensure_ascii=False, separators=(',', ':'))
		new_line = f'ANYROUTER_ACCOUNTS={new_accounts_json}'

		lines = content.split('\n')
		updated = False
		for i, line in enumerate(lines):
			if line.startswith('ANYROUTER_ACCOUNTS='):
				lines[i] = new_line
				updated = True
				break

		if not updated:
			lines.insert(1, new_line)

		with open(env_file, 'w', encoding='utf-8') as f:
			f.write('\n'.join(lines))

		print(f'[SUCCESS] Updated ANYROUTER_ACCOUNTS in {env_file}')
		return True

	except Exception as e:
		print(f'[FAILED] Failed to update {env_file}: {e}')
		return False


async def _poll_session_cookie(context, domain: str, label: str, timeout: int) -> dict | None:
	"""轮询等待目标域名出现 session cookie

	Args:
		context: Playwright BrowserContext
		domain: 目标域名 (e.g. "https://anyrouter.top")
		label: 日志标签
		timeout: 最大等待秒数

	Returns:
		该域名下所有 cookie 的 {name: value} 字典，超时返回 None
	"""
	import asyncio
	import time

	start = time.monotonic()
	last_msg_time = start

	while time.monotonic() - start < timeout:
		all_cookies = await context.cookies(domain)
		cookies_dict = {}
		for c in all_cookies:
			name = c.get('name', '')
			value = c.get('value', '')
			if name and value:
				cookies_dict[name] = value

		if 'session' in cookies_dict and len(cookies_dict['session']) > 20:
			elapsed = int(time.monotonic() - start)
			print(f'  [SUCCESS] {label}: session cookie captured ({elapsed}s)')
			return cookies_dict

		# 每 30 秒打印一次等待提示
		now = time.monotonic()
		if now - last_msg_time >= 30:
			remaining = int(timeout - (now - start))
			print(f'  [WAITING] {label}: Still waiting for login... ({remaining}s remaining)')
			last_msg_time = now

		await asyncio.sleep(POLL_INTERVAL)

	return None


async def _check_linuxdo_logged_in(context) -> bool:
	"""检查持久化 context 中是否已有 LinuxDo 登录态

	通过检查 linux.do 域名下是否存在 _t cookie（Discourse 登录标志）来判断。
	"""
	try:
		cookies = await context.cookies('https://linux.do')
		cookie_names = {c.get('name', '') for c in cookies}
		# Discourse 登录后会有 _t cookie
		return '_t' in cookie_names
	except Exception:
		return False


async def _refresh_in_shared_browser(
	failed_accounts: list[dict],
	accounts_data: list[dict],
) -> tuple[int, set]:
	"""在共享的持久化浏览器中，依次为所有失败账号刷新 cookie

	流程：
	1. 启动持久化 context（保留 LinuxDo 登录态）
	2. 检测 LinuxDo 是否已登录
	   - 已登录：直接开始，每个站点自动 OAuth 回调
	   - 未登录：先导航到 linux.do 让用户登录一次
	3. 逐个站点：打开标签页 → 导航到登录页 → 等待 session cookie → 关闭标签页
	4. 所有站点完成后关闭浏览器

	Returns:
		(refreshed_count, updated_indices)
	"""
	import asyncio

	total = len(failed_accounts)
	refreshed_count = 0
	updated_indices = set()

	async with async_playwright() as p:
		# 使用持久化用户数据目录，LinuxDo 登录态跨次运行保留
		context = await p.chromium.launch_persistent_context(
			user_data_dir=BROWSER_DATA_DIR,
			headless=False,
			user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
			viewport={'width': 1280, 'height': 900},
			args=[
				'--disable-blink-features=AutomationControlled',
				'--disable-dev-shm-usage',
				'--no-sandbox',
			],
		)

		try:
			# ====== 检查 LinuxDo 登录态 ======
			linuxdo_logged_in = await _check_linuxdo_logged_in(context)

			if linuxdo_logged_in:
				print('[INFO] LinuxDo 已登录（持久化 session 有效），将自动完成 OAuth 回调')
			else:
				print('\n[BROWSER] LinuxDo 未登录，请在浏览器中登录 LinuxDo（仅需一次）')
				print('[BROWSER] 登录完成后，后续所有站点将自动完成 OAuth 认证')

				page = await context.new_page()
				try:
					await page.goto('https://linux.do', wait_until='domcontentloaded', timeout=30000)
				except Exception as e:
					print(f'[WARNING] LinuxDo page load issue: {str(e)[:80]}')

				# 等待用户登录 LinuxDo
				print(f'[BROWSER] 等待 LinuxDo 登录... (最长 {MANUAL_LOGIN_TIMEOUT}s)')
				import time
				start = time.monotonic()
				while time.monotonic() - start < MANUAL_LOGIN_TIMEOUT:
					if await _check_linuxdo_logged_in(context):
						print('[SUCCESS] LinuxDo 登录成功！')
						linuxdo_logged_in = True
						break
					await asyncio.sleep(POLL_INTERVAL)

				if not linuxdo_logged_in:
					print('[TIMEOUT] LinuxDo 登录超时，跳过 Cookie 刷新')
					await page.close()
					await context.close()
					return 0, set()

				# 登录成功后等一下让 cookie 稳定
				await asyncio.sleep(1)
				await page.close()

			# ====== 逐个刷新失败站点 ======
			for seq, acc in enumerate(failed_accounts, 1):
				label = f'[{acc["provider"]}] {acc["name"]}'
				login_url = f'{acc["domain"]}{acc["login_path"]}'

				print(f'\n{"─" * 60}')
				print(f'🔄 [{seq}/{total}] {label}')
				print(f'   {login_url}')
				print(f'{"─" * 60}')

				page = await context.new_page()

				try:
					await page.goto(login_url, wait_until='domcontentloaded', timeout=30000)
				except Exception as e:
					print(f'  [WARNING] Page load issue (may still work): {str(e)[:80]}')

				# 第一阶段：快速等待（OAuth 自动回调，通常 5~15s）
				cookies = await _poll_session_cookie(
					context, acc['domain'], label, timeout=OAUTH_AUTO_TIMEOUT,
				)

				if not cookies:
					# 第二阶段：自动回调未成功，可能需要用户手动操作
					print(f'  [INFO] {label}: 自动 OAuth 未完成，请在浏览器中手动完成登录')
					print(f'  [INFO] 等待手动登录... (最长 {MANUAL_LOGIN_TIMEOUT}s)')
					cookies = await _poll_session_cookie(
						context, acc['domain'], label, timeout=MANUAL_LOGIN_TIMEOUT,
					)

				# 关闭当前站点标签页（不影响 context 中的 cookie）
				await page.close()

				if cookies:
					# 更新 accounts_data
					idx = acc['index']
					old_cookies = accounts_data[idx].get('cookies', {})

					if isinstance(old_cookies, dict):
						updated_cookies = {}
						for key in old_cookies:
							if key in cookies:
								updated_cookies[key] = cookies[key]
							else:
								updated_cookies[key] = old_cookies[key]
						if 'session' in cookies and 'session' not in updated_cookies:
							updated_cookies['session'] = cookies['session']
					else:
						updated_cookies = {'session': cookies.get('session', '')}

					accounts_data[idx]['cookies'] = updated_cookies
					updated_indices.add(idx)
					refreshed_count += 1
					print(f'  [SUCCESS] {label}: Cookie 已更新 ✓')
				else:
					print(f'  [FAILED] {label}: Cookie 刷新失败（超时）✗')

		finally:
			await context.close()

	return refreshed_count, updated_indices


async def refresh_failed_accounts(
	failed_accounts: list[dict],
	accounts_data: list[dict],
	providers_config: dict,
	auto_yes: bool = False,
	env_file: str = '.env',
) -> int:
	"""为签到失败的账号刷新 Cookie

	核心特性：
	- 使用持久化浏览器数据目录，LinuxDo 登录态跨次运行保留
	- 所有失败站点共享同一个浏览器实例，用户只需登录 LinuxDo 一次
	- 如果 LinuxDo 已登录，OAuth 回调自动完成，无需用户操作

	Args:
		failed_accounts: 失败账号信息列表
		accounts_data: 完整的账号数据列表（会被就地修改）
		providers_config: 提供商配置字典
		auto_yes: 是否跳过确认直接开始（-y 参数）
		env_file: .env 文件路径

	Returns:
		成功刷新的账号数量
	"""
	if not failed_accounts:
		return 0

	# ====== 显示失败名单 ======
	print('\n' + '=' * 60)
	print('⚠️  以下账号签到失败（可能 session 已过期）：')
	print('-' * 60)
	for i, acc in enumerate(failed_accounts, 1):
		print(f'  {i}. [{acc["provider"]}] {acc["name"]} (api_user: {acc["api_user"]})')
		print(f'     🌐 {acc["domain"]}')
	print('=' * 60)

	# ====== 确认是否刷新 ======
	if not auto_yes:
		try:
			answer = input(
				'\n🔄 是否打开浏览器刷新 Cookie？\n'
				'   所有站点共享同一个浏览器，LinuxDo 只需登录一次\n'
				f'   (浏览器数据保存在 {BROWSER_DATA_DIR})\n'
				'   [y/N]: '
			).strip().lower()
			if answer not in ('y', 'yes'):
				print('[INFO] User declined cookie refresh')
				return 0
		except (KeyboardInterrupt, EOFError):
			print('\n[INFO] Cookie refresh cancelled')
			return 0

	# ====== 在共享浏览器中刷新所有账号 ======
	refreshed_count, updated_indices = await _refresh_in_shared_browser(
		failed_accounts, accounts_data,
	)

	# ====== 写入 .env ======
	total = len(failed_accounts)
	if updated_indices:
		if update_env_accounts(accounts_data, env_file):
			print(f'\n✅ 成功刷新 {refreshed_count}/{total} 个账号')
			print(f'📁 已更新 {env_file}')
		else:
			print(f'\n⚠️  刷新了 {refreshed_count} 个 Cookie 但写入 {env_file} 失败')
	else:
		print(f'\n❌ {total} 个账号全部刷新失败')

	return refreshed_count
