#!/usr/bin/env python3
"""
Cookie 刷新模块

通过 Playwright 打开有头浏览器，让用户手动完成登录（OAuth / LinuxDo 等第三方认证），
自动检测登录完成后抓取新的 session cookie，更新 .env 配置。

每个失败账号独立开启/销毁一个浏览器实例，避免 cookie 互相污染。
"""

import json

from playwright.async_api import async_playwright

# 用户手动登录的最大等待时间（秒）
LOGIN_TIMEOUT = 300
# 轮询 cookie 的间隔（秒）
POLL_INTERVAL = 2


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


async def _wait_for_session_cookie(context, domain: str, label: str, timeout: int = LOGIN_TIMEOUT) -> dict | None:
	"""轮询等待 session cookie 出现

	登录完成的判断标准：该域名下出现名为 "session" 的 cookie。

	Args:
		context: Playwright BrowserContext
		domain: 目标域名
		label: 日志标签
		timeout: 最大等待秒数

	Returns:
		包含该域名所有 cookie 的字典，超时返回 None
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
			print(f'\n[SUCCESS] {label}: Login detected! (session cookie captured in {elapsed}s)')
			return cookies_dict

		# 每 30 秒打印一次等待提示
		now = time.monotonic()
		if now - last_msg_time >= 30:
			remaining = int(timeout - (now - start))
			print(f'[WAITING] {label}: Still waiting for login... ({remaining}s remaining)')
			last_msg_time = now

		await asyncio.sleep(POLL_INTERVAL)

	print(f'\n[TIMEOUT] {label}: Login timeout after {timeout}s')
	return None


async def refresh_single_account(
	domain: str,
	login_path: str,
	account_label: str,
	timeout: int = LOGIN_TIMEOUT,
) -> dict | None:
	"""为单个账号打开浏览器，等待用户手动登录，抓取 cookie 后关闭

	流程：
	1. 启动有头浏览器（用户可见）
	2. 导航到登录页
	3. 终端提示用户在浏览器中完成登录
	4. 后台轮询检测 session cookie
	5. 检测到 → 抓取所有 cookie → 关闭浏览器
	6. 超时 → 关闭浏览器 → 返回 None

	Args:
		domain: 网站域名
		login_path: 登录页面路径
		account_label: 显示标签
		timeout: 最大等待秒数

	Returns:
		新的 cookies 字典，失败返回 None
	"""
	login_url = f'{domain}{login_path}'
	print(f'\n[BROWSER] {account_label}: Opening browser → {login_url}')
	print(f'[BROWSER] {account_label}: Please complete login in the browser window.')
	print(f'[BROWSER] {account_label}: Waiting up to {timeout}s for login...')

	browser = None
	context = None

	try:
		async with async_playwright() as p:
			# 有头模式启动，用户可以看到并操作
			browser = await p.chromium.launch(
				headless=False,
				args=[
					'--disable-blink-features=AutomationControlled',
					'--disable-dev-shm-usage',
					'--no-sandbox',
				],
			)

			context = await browser.new_context(
				user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
				viewport={'width': 1280, 'height': 900},
			)

			page = await context.new_page()

			try:
				await page.goto(login_url, wait_until='domcontentloaded', timeout=30000)
			except Exception as e:
				print(f'[WARNING] {account_label}: Page load issue (may still work): {str(e)[:80]}')

			# 轮询等待 session cookie
			cookies = await _wait_for_session_cookie(context, domain, account_label, timeout)

			# 不管成功失败都关闭
			await context.close()
			await browser.close()
			return cookies

	except Exception as e:
		print(f'[FAILED] {account_label}: Browser error - {e}')
		# 确保清理
		try:
			if context:
				await context.close()
			if browser:
				await browser.close()
		except Exception:
			pass
		return None


async def refresh_failed_accounts(
	failed_accounts: list[dict],
	accounts_data: list[dict],
	providers_config: dict,
	auto_yes: bool = False,
	env_file: str = '.env',
) -> int:
	"""为签到失败的账号逐一打开浏览器，让用户手动登录并抓取新 cookie

	Args:
		failed_accounts: 失败账号信息列表，每项包含:
			- index: 账号在 accounts_data 中的索引
			- provider: 提供商名称
			- api_user: API 用户 ID
			- name: 显示名称
			- domain: 域名
			- login_path: 登录路径
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
				'\n🔄 是否打开浏览器手动登录刷新 Cookie？\n'
				'   (将逐一为每个失败账号打开浏览器，您需要在浏览器中完成登录)\n'
				'   [y/N]: '
			).strip().lower()
			if answer not in ('y', 'yes'):
				print('[INFO] User declined cookie refresh')
				return 0
		except (KeyboardInterrupt, EOFError):
			print('\n[INFO] Cookie refresh cancelled')
			return 0

	# ====== 逐一刷新 ======
	refreshed_count = 0
	updated_indices = set()
	total = len(failed_accounts)

	for seq, acc in enumerate(failed_accounts, 1):
		label = f'[{acc["provider"]}] {acc["name"]}'

		print(f'\n{"─" * 60}')
		print(f'🔄 [{seq}/{total}] Refreshing: {label}')
		print(f'   Domain: {acc["domain"]}')
		print(f'   Login:  {acc["domain"]}{acc["login_path"]}')
		print(f'{"─" * 60}')

		# 每个账号独立开启/销毁浏览器
		new_cookies = await refresh_single_account(
			domain=acc['domain'],
			login_path=acc['login_path'],
			account_label=label,
		)

		if new_cookies:
			# 更新 accounts_data 中对应账号的 cookies
			idx = acc['index']
			old_cookies = accounts_data[idx].get('cookies', {})

			if isinstance(old_cookies, dict):
				# 保留原有 cookie 键名结构，用新值覆盖
				updated_cookies = {}
				for key in old_cookies:
					if key in new_cookies:
						updated_cookies[key] = new_cookies[key]
					else:
						updated_cookies[key] = old_cookies[key]
				# 确保 session 在里面
				if 'session' in new_cookies and 'session' not in updated_cookies:
					updated_cookies['session'] = new_cookies['session']
			else:
				updated_cookies = {'session': new_cookies.get('session', '')}

			accounts_data[idx]['cookies'] = updated_cookies
			updated_indices.add(idx)
			refreshed_count += 1
			print(f'[SUCCESS] {label}: Cookie updated ✓')
		else:
			print(f'[FAILED] {label}: Cookie refresh failed ✗')

		# 如果还有下一个，给用户一点缓冲时间
		if seq < total:
			try:
				input(f'\n  ⏎ Press Enter to continue to next account ({seq}/{total} done)...')
			except (KeyboardInterrupt, EOFError):
				print(f'\n[INFO] Remaining {total - seq} account(s) skipped by user')
				break

	# ====== 写入 .env ======
	if updated_indices:
		if update_env_accounts(accounts_data, env_file):
			print(f'\n✅ Successfully refreshed {refreshed_count}/{total} account(s)')
			print(f'📁 Updated {env_file} with new cookies')
		else:
			print(f'\n⚠️  Refreshed {refreshed_count} cookie(s) but failed to save to {env_file}')

	return refreshed_count
