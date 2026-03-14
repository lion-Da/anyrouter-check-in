#!/usr/bin/env python3
"""
Cookie 刷新模块

核心设计：
1. 使用 Playwright 持久化用户数据目录（~/.anyrouter-browser-data），
   LinuxDo 的登录态跨次运行保留，用户只需首次登录一次。
2. 所有失败站点共享同一个浏览器实例（同一个 BrowserContext），
   浏览器按域名天然隔离 cookie，LinuxDo OAuth 登录态自动复用。
3. 手动触发模式：
   脚本打开站点登录页 → 用户在浏览器中完成 OAuth 登录
   → 用户回到终端按 Enter → 脚本从浏览器提取 session cookie
   → 更新配置。输入 s 跳过当前账号。
"""

import asyncio
import json
import os
import sys
import threading

from playwright.async_api import async_playwright

# 持久化浏览器用户数据目录（保存 LinuxDo 登录态等）
BROWSER_DATA_DIR = os.path.join(os.path.expanduser('~'), '.anyrouter-browser-data')

# 等待用户操作的最大时间（秒）
LOGIN_TIMEOUT = 600


# ────────────────────────────────────────────────────────────
# 全局唯一 stdin 监听线程
# ────────────────────────────────────────────────────────────

class _StdinCommandMonitor:
	"""全局唯一的 stdin 监听器

	后台线程持续读取 stdin 输入行，将用户输入内容传递给当前活跃的回调。
	支持区分不同输入（Enter=确认提取, s=跳过）。
	"""

	def __init__(self):
		self._lock = threading.Lock()
		self._loop: asyncio.AbstractEventLoop | None = None
		self._future: asyncio.Future | None = None
		self._thread: threading.Thread | None = None
		self._started = False

	def _reader_loop(self):
		"""后台线程：持续读 stdin，将输入内容解析后投递到 asyncio Future"""
		while True:
			try:
				line = sys.stdin.readline()
			except (EOFError, OSError):
				return
			if line is None:
				return

			text = line.strip().lower()

			with self._lock:
				if self._future is not None and self._loop is not None:
					fut = self._future
					lp = self._loop
					# 区分：空行(Enter) = "confirm", "s" = "skip"
					if text in ('s', 'skip'):
						lp.call_soon_threadsafe(fut.set_result, 'skip')
					else:
						lp.call_soon_threadsafe(fut.set_result, 'confirm')
					# 一次性消费：防止重复触发
					self._future = None

	def _ensure_started(self):
		if self._started:
			return
		self._started = True
		self._thread = threading.Thread(target=self._reader_loop, daemon=True)
		self._thread.start()

	def wait_for_input(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future:
		"""注册一个新的 Future，等待用户下一次输入

		Returns:
			Future[str]，结果为 "confirm" 或 "skip"
		"""
		self._ensure_started()
		future = loop.create_future()
		with self._lock:
			self._loop = loop
			self._future = future
		return future

	def cancel(self):
		"""取消当前等待"""
		with self._lock:
			if self._future is not None and not self._future.done():
				lp = self._loop
				fut = self._future
				if lp:
					lp.call_soon_threadsafe(fut.cancel)
			self._future = None


# 模块级单例
_stdin_monitor = _StdinCommandMonitor()


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


async def _wait_user_command(label: str, timeout: int) -> str:
	"""等待用户在终端中输入命令

	阻塞等待用户按键：
	  - Enter（空行）→ 返回 "confirm"（提取 cookie）
	  - 输入 s → 返回 "skip"（跳过当前账号）

	Args:
		label: 日志标签
		timeout: 最大等待秒数

	Returns:
		"confirm" | "skip" | "timeout"
	"""
	loop = asyncio.get_running_loop()
	future = _stdin_monitor.wait_for_input(loop)

	try:
		result = await asyncio.wait_for(future, timeout=timeout)
		return result
	except asyncio.TimeoutError:
		_stdin_monitor.cancel()
		print(f'  [TIMEOUT] {label}: 等待超时 ({timeout}s)')
		return 'timeout'
	except asyncio.CancelledError:
		return 'timeout'


async def _extract_session_cookie(
	context,
	domain: str,
	label: str,
) -> dict | None:
	"""从浏览器 context 中一次性提取目标域名的 cookie

	由用户按 Enter 触发调用，不做任何轮询。

	Args:
		context: Playwright BrowserContext
		domain: 目标域名 (e.g. "https://anyrouter.top")
		label: 日志标签

	Returns:
		该域名下所有 cookie 的 {name: value} 字典，无有效 session 返回 None
	"""
	try:
		all_cookies = await context.cookies(domain)
	except Exception as e:
		print(f'  [ERROR] {label}: 读取 cookie 失败: {e}')
		return None

	cookies_dict = {}
	for c in all_cookies:
		name = c.get('name', '')
		value = c.get('value', '')
		if name and value:
			cookies_dict[name] = value

	if 'session' in cookies_dict and len(cookies_dict['session']) > 20:
		session_preview = cookies_dict['session'][:16] + '...'
		print(f'  [SUCCESS] {label}: session cookie 已提取 ({session_preview})')
		return cookies_dict

	# session 不存在或太短
	if 'session' in cookies_dict:
		print(f'  [FAILED] {label}: session cookie 存在但无效 (长度={len(cookies_dict["session"])})')
	else:
		available = list(cookies_dict.keys())[:10]
		print(f'  [FAILED] {label}: 未找到 session cookie (可用: {available})')

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


async def _close_extra_pages(context):
	"""关闭所有标签页，只保留一个空白页

	Chromium persistent context 在所有 page 关闭后会自动销毁，
	所以必须先创建一个空白页再关闭其他页面，确保 context 始终存活。
	"""
	# 先开一个空白页保底，防止 context 因 0 page 而自毁
	blank = await context.new_page()

	for pg in list(context.pages):
		if pg == blank:
			continue
		try:
			await pg.close()
		except Exception:
			pass


async def _refresh_in_shared_browser(
	failed_accounts: list[dict],
	accounts_data: list[dict],
) -> tuple[int, set]:
	"""在共享的持久化浏览器中，依次为所有失败账号刷新 cookie

	交互流程（手动触发模式）：
	1. 启动持久化 context（保留 LinuxDo 登录态）
	2. 检测 LinuxDo 是否已登录
	   - 已登录：提示用户，直接开始
	   - 未登录：导航到 linux.do，用户登录后按 Enter 确认
	3. 逐个站点：
	   a. 新标签页 → 导航到站点登录页
	   b. 用户在浏览器中完成 OAuth 登录（可能涉及新 tab）
	   c. 用户回到终端：
	      - 按 Enter → 脚本从浏览器提取 session cookie
	      - 输入 s → 跳过当前账号
	   d. 如果提取失败，可以重试（再按 Enter）或跳过（输入 s）
	   e. 关闭多余标签页 → 下一个站点
	4. 所有站点完成后关闭浏览器

	Returns:
		(refreshed_count, updated_indices)
	"""
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
			else:
				print('\n[BROWSER] LinuxDo 未登录，请先在浏览器中登录 LinuxDo')
				print('[BROWSER] 登录完成后回到终端按 Enter 继续，输入 s 跳过')

				page = await context.new_page()
				try:
					await page.goto('https://linux.do', wait_until='domcontentloaded', timeout=30000)
				except Exception as e:
					print(f'[WARNING] LinuxDo 页面加载异常: {str(e)[:80]}')

				cmd = await _wait_user_command('LinuxDo 登录', LOGIN_TIMEOUT)

				if cmd == 'confirm':
					if await _check_linuxdo_logged_in(context):
						print('[SUCCESS] LinuxDo 登录确认成功！')
						linuxdo_logged_in = True
					else:
						print('[WARNING] LinuxDo 似乎还未登录，将继续尝试各站点')
				elif cmd == 'skip':
					print('[SKIP] 跳过 LinuxDo 登录')
				else:
					print('[TIMEOUT] LinuxDo 登录等待超时')

				await _close_extra_pages(context)

			# ====== 逐个刷新失败站点 ======
			for seq, acc in enumerate(failed_accounts, 1):
				label = f'[{acc["provider"]}] {acc["name"]}'
				login_url = f'{acc["domain"]}{acc["login_path"]}'

				print(f'\n{"─" * 60}')
				print(f'🔄 [{seq}/{total}] {label}')
				print(f'   {login_url}')
				print(f'{"─" * 60}')
				print(f'  📋 请在浏览器中完成 OAuth 登录')
				print(f'  ⏎  完成后按 Enter 提取 cookie | 输入 s 跳过')

				page = await context.new_page()

				try:
					await page.goto(login_url, wait_until='domcontentloaded', timeout=30000)
				except Exception as e:
					print(f'  [WARNING] 页面加载异常 (可能仍可操作): {str(e)[:80]}')

				# ---- 等待用户操作，支持重试 ----
				cookies = None
				while True:
					cmd = await _wait_user_command(label, LOGIN_TIMEOUT)

					if cmd == 'skip':
						print(f'  [SKIP] {label}: 用户跳过')
						break
					elif cmd == 'timeout':
						break
					else:
						# cmd == 'confirm': 用户按了 Enter，提取 cookie
						cookies = await _extract_session_cookie(
							context=context,
							domain=acc['domain'],
							label=label,
						)

						if cookies:
							break
						else:
							# 提取失败，允许用户重试
							print(f'  ⚠️  cookie 提取失败，请确认已在浏览器中完成登录')
							print(f'  ⏎  再次按 Enter 重试 | 输入 s 跳过')

				# 关闭当前站点相关的所有标签页（OAuth 可能开了新 tab）
				await _close_extra_pages(context)

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
