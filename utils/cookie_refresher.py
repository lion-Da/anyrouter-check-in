#!/usr/bin/env python3
"""
Cookie 刷新模块

核心设计：
1. 使用 Playwright 持久化用户数据目录（~/.anyrouter-browser-data），
   LinuxDo 的登录态跨次运行保留，用户只需首次登录一次。
2. 所有失败站点共享同一个浏览器实例（同一个 BrowserContext），
   浏览器按域名天然隔离 cookie，LinuxDo OAuth 登录态自动复用。
3. 完整的 OAuth 流程：
   导航到站点登录页 → 用户在浏览器中完成 LinuxDo OAuth 认证
   → 回调到站点 → 用户完成"连接/绑定"操作 → 站点生成 session cookie
   → 脚本检测到 session cookie → 抓取并更新配置。
4. 等待过程中随时可按 Enter 跳过当前账号。
"""

import asyncio
import json
import os
import sys
import threading

from playwright.async_api import async_playwright

# 持久化浏览器用户数据目录（保存 LinuxDo 登录态等）
BROWSER_DATA_DIR = os.path.join(os.path.expanduser('~'), '.anyrouter-browser-data')

# 等待用户完成登录+连接的最大时间（秒）
LOGIN_TIMEOUT = 300
# 轮询 cookie 的间隔（秒）
POLL_INTERVAL = 1.5


# ────────────────────────────────────────────────────────────
# 全局唯一 stdin 监听线程（解决多线程竞争 stdin 的问题）
# ────────────────────────────────────────────────────────────

class _StdinSkipMonitor:
	"""全局唯一的 stdin Enter 监听器

	只有一个后台线程持续读取 stdin，每次按 Enter 都设置当前活跃的 event。
	外部通过 arm() 注册新的 asyncio.Event，通过 disarm() 解除。
	"""

	def __init__(self):
		self._lock = threading.Lock()
		self._loop: asyncio.AbstractEventLoop | None = None
		self._event: asyncio.Event | None = None
		self._thread: threading.Thread | None = None
		self._started = False

	def _reader_loop(self):
		"""后台线程：持续读 stdin，每收到一行就 set 当前 event"""
		while True:
			try:
				sys.stdin.readline()
			except (EOFError, OSError):
				# stdin 被关闭（如 pipe 模式），停止监听
				return
			with self._lock:
				if self._event is not None and self._loop is not None:
					ev = self._event
					lp = self._loop
					lp.call_soon_threadsafe(ev.set)

	def _ensure_started(self):
		"""确保后台线程只启动一次"""
		if self._started:
			return
		self._started = True
		self._thread = threading.Thread(target=self._reader_loop, daemon=True)
		self._thread.start()

	def arm(self, loop: asyncio.AbstractEventLoop) -> asyncio.Event:
		"""注册一个新的 skip event，返回供 poll 循环检查

		Args:
			loop: 当前事件循环

		Returns:
			asyncio.Event，被 Enter 触发后 is_set() 为 True
		"""
		self._ensure_started()
		event = asyncio.Event()
		with self._lock:
			self._loop = loop
			self._event = event
		return event

	def disarm(self):
		"""解除当前 event，避免残留触发下一轮"""
		with self._lock:
			self._event = None


# 模块级单例
_skip_monitor = _StdinSkipMonitor()


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


async def _poll_login_complete(
	page,
	context,
	domain: str,
	login_path: str,
	label: str,
	timeout: int,
	skip_event: asyncio.Event | None = None,
) -> dict | None:
	"""轮询等待登录完成：页面离开 login 路径 + session cookie 有效

	OAuth 登录的完整流程：
	  login 页 → 跳转 LinuxDo → 回调 → 站点显示"连接/绑定" → 用户确认
	  → 站点后端写入 session cookie → 页面跳转到首页/dashboard

	仅检查 session cookie 不够——OAuth 中间过程也可能有临时 cookie。
	真正的完成标志是：
	  1. 页面 URL 不再是 login 路径（已跳转到站点其他页面）
	  2. 该域名下存在有效的 session cookie

	等待过程中可按 Enter 跳过当前账号。

	Args:
		page: 当前打开的 Playwright Page（用于检测 URL）
		context: Playwright BrowserContext（用于读 cookie）
		domain: 目标域名 (e.g. "https://anyrouter.top")
		login_path: 登录路径 (e.g. "/login" 或 "/auth/login")
		label: 日志标签
		timeout: 最大等待秒数
		skip_event: 按 Enter 时会被 set 的事件，为 None 则不支持跳过

	Returns:
		该域名下所有 cookie 的 {name: value} 字典，超时/跳过返回 None
	"""
	import time
	from urllib.parse import urlparse

	# 预计算所有需要视为"仍在登录中"的路径前缀
	login_prefixes = set()
	login_prefixes.add(login_path.rstrip('/'))  # e.g. "/login", "/auth/login"
	# 也把 linux.do、connect.linux.do 等 OAuth 中间页算在内
	oauth_domains = {'linux.do', 'connect.linux.do'}

	start = time.monotonic()
	last_msg_time = start
	last_url = ''

	while time.monotonic() - start < timeout:
		# 检查是否被用户跳过
		if skip_event and skip_event.is_set():
			print(f'  [SKIP] {label}: 用户按下 Enter，跳过当前账号')
			return None

		# ---- 检查当前页面 URL ----
		try:
			current_url = page.url
		except Exception:
			current_url = ''

		# 打印 URL 变化（帮助用户和调试）
		if current_url != last_url:
			short_url = current_url[:100] + ('...' if len(current_url) > 100 else '')
			print(f'  [URL] {short_url}')
			last_url = current_url

		parsed = urlparse(current_url)
		current_path = parsed.path.rstrip('/')
		current_host = parsed.hostname or ''

		# 判断是否还在登录/OAuth 流程中
		still_on_login = False
		if current_host in oauth_domains:
			# 还在 LinuxDo OAuth 页面
			still_on_login = True
		elif any(current_path == prefix or current_path.startswith(prefix + '/') for prefix in login_prefixes):
			# 还在站点的 login 路径
			still_on_login = True
		elif not current_url or current_url == 'about:blank':
			still_on_login = True

		if not still_on_login:
			# 页面已离开 login，检查 session cookie
			all_cookies = await context.cookies(domain)
			cookies_dict = {}
			for c in all_cookies:
				name = c.get('name', '')
				value = c.get('value', '')
				if name and value:
					cookies_dict[name] = value

			if 'session' in cookies_dict and len(cookies_dict['session']) > 20:
				elapsed = int(time.monotonic() - start)
				print(f'  [SUCCESS] {label}: 登录完成，session cookie 已捕获 ({elapsed}s)')
				return cookies_dict

		# 每 30 秒打印一次等待提示
		now = time.monotonic()
		if now - last_msg_time >= 30:
			remaining = int(timeout - (now - start))
			print(f'  [WAITING] {label}: 仍在等待登录完成... (剩余 {remaining}s，按 Enter 跳过)')
			last_msg_time = now

		await asyncio.sleep(POLL_INTERVAL)

	print(f'  [TIMEOUT] {label}: 等待超时 ({timeout}s)')
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

	完整流程：
	1. 启动持久化 context（保留 LinuxDo 登录态）
	2. 检测 LinuxDo 是否已登录
	   - 已登录：提示用户，直接开始
	   - 未登录：导航到 linux.do 让用户登录一次
	3. 逐个站点：
	   a. 新标签页 → 导航到站点登录页
	   b. 用户在浏览器中完成 OAuth 认证 + 连接/绑定
	   c. 轮询等待站点域名下出现 session cookie
	   d. 等待过程中可按 Enter 跳过当前账号
	   e. 关闭标签页 → 下一个站点
	4. 所有站点完成后关闭浏览器

	Returns:
		(refreshed_count, updated_indices)
	"""
	import time

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
				print('[INFO] LinuxDo 已登录（持久化 session 有效）')
				print('[INFO] 每个站点仍需在浏览器中完成 OAuth 授权 + 连接操作')
			else:
				print('\n[BROWSER] LinuxDo 未登录，请先在浏览器中登录 LinuxDo')
				print('[BROWSER] 登录完成后，后续站点可通过 OAuth 快速认证')

				page = await context.new_page()
				try:
					await page.goto('https://linux.do', wait_until='domcontentloaded', timeout=30000)
				except Exception as e:
					print(f'[WARNING] LinuxDo 页面加载异常: {str(e)[:80]}')

				# 等待用户登录 LinuxDo（支持 Enter 跳过）
				print(f'[BROWSER] 等待 LinuxDo 登录... (最长 {LOGIN_TIMEOUT}s，按 Enter 跳过)')
				loop = asyncio.get_running_loop()
				skip_event = _skip_monitor.arm(loop)

				start = time.monotonic()
				while time.monotonic() - start < LOGIN_TIMEOUT:
					if skip_event.is_set():
						print('[SKIP] 用户跳过 LinuxDo 登录')
						break
					if await _check_linuxdo_logged_in(context):
						print('[SUCCESS] LinuxDo 登录成功！')
						linuxdo_logged_in = True
						break
					await asyncio.sleep(POLL_INTERVAL)

				_skip_monitor.disarm()

				if not linuxdo_logged_in and not skip_event.is_set():
					print('[TIMEOUT] LinuxDo 登录超时')

				# 登录成功后稍等让 cookie 稳定
				if linuxdo_logged_in:
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
				print(f'  📋 请在浏览器中完成：OAuth 登录 → 连接/绑定账号')
				print(f'  ⏎  按 Enter 跳过当前账号')

				page = await context.new_page()

				try:
					await page.goto(login_url, wait_until='domcontentloaded', timeout=30000)
				except Exception as e:
					print(f'  [WARNING] 页面加载异常 (可能仍可操作): {str(e)[:80]}')

				# 注册 Enter 跳过监听（复用全局唯一线程，替换 event）
				loop = asyncio.get_running_loop()
				skip_event = _skip_monitor.arm(loop)

				# 等待用户在浏览器中完成完整的登录流程（OAuth + 连接 + 页面跳转）
				cookies = await _poll_login_complete(
					page=page,
					context=context,
					domain=acc['domain'],
					login_path=acc['login_path'],
					label=label,
					timeout=LOGIN_TIMEOUT,
					skip_event=skip_event,
				)

				# 解除当前 event，防止残留触发影响下一轮
				_skip_monitor.disarm()

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
					print(f'  ✅ {label}: Cookie 已更新')
				else:
					print(f'  ❌ {label}: Cookie 刷新失败')

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
	- 等待过程中可按 Enter 跳过当前账号
	- 站点需完成完整 OAuth 流程（认证 + 连接/绑定）后才会生成有效 session

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
