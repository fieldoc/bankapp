-- BankApp.app — launches the local finance dashboard as a real macOS app.
--
-- Compiled as a STAY-OPEN applet (osacompile -s), so it lives in the Dock after
-- launching and its `on quit` handler runs when you Cmd-Q / Dock-Quit it:
--   • on run  — free port 8377 (kill any stale server) then start `finance serve`
--                from the stable ~/BankApp install; the server opens the browser itself.
--   • on quit — stop the server (by PID and by sweeping the port), then really quit.
--
-- Paths use $HOME (not a hardcoded user) and the stable ~/BankApp install, NOT whatever
-- worktree built this — so the app keeps working after the build worktree is gone.
-- Rebuild with scripts/mac-app/build.sh.

property dashPort : "8377"
property dashURL : "http://127.0.0.1:8377/"
property serverPID : ""

-- Kill whatever is LISTENing on the dashboard port. -sTCP:LISTEN so we only ever
-- target a server holding the port, never an incidental client connection.
on portSweepCmd()
	return "/usr/sbin/lsof -ti tcp:" & dashPort & " -sTCP:LISTEN | /usr/bin/xargs /bin/kill -9 2>/dev/null; true"
end portSweepCmd

-- True when something is already LISTENing on the dashboard port.
on serverIsUp()
	set n to do shell script "/usr/sbin/lsof -ti tcp:" & dashPort & " -sTCP:LISTEN | /usr/bin/wc -l | /usr/bin/tr -d ' '"
	return n is not "0"
end serverIsUp

on run
	-- Auto-kill any old server first, so the fresh one can bind (and open the browser;
	-- `finance serve` prints "could not bind" and does NOT open the browser if the port
	-- is taken, so freeing it here is what makes relaunch Just Work).
	do shell script portSweepCmd()
	-- `&` must apply to the nohup ALONE, and every fd of the backgrounded child must point
	-- away from `do shell script`'s pipe. Written the tempting way — `cd X && nohup Y >log &`
	-- — the shell backgrounds the whole AND-list in a SUBSHELL: the redirection binds to
	-- nohup, so the subshell itself keeps the pipe's write end open. Two failures follow:
	--   • `do shell script` blocks waiting for EOF that only arrives when the server dies,
	--     so `on run` never returns, the applet never reaches its event loop, and Cmd-Q
	--     times out (-1712) without ever entering `on quit`;
	--   • $! is the subshell's pid, not the server's, so `kill $!` reaps the wrapper and
	--     orphans the real server.
	-- Ending the `cd` with `;` keeps nohup a simple command, which the forked child execs
	-- (so $! IS the server), and its own redirections detach all three fds.
	set startCmd to "mkdir -p \"$HOME/finance/logs\"; cd \"$HOME/BankApp\" || exit 1; /usr/bin/nohup \"$HOME/BankApp/.venv/bin/finance\" serve > \"$HOME/finance/logs/webapp.log\" 2>&1 < /dev/null & echo $!"
	set serverPID to do shell script startCmd
end run

-- Clicking the Dock/Spotlight icon while the app is ALREADY running sends `reopen`, not
-- `run`. Without this the click does nothing visible (a stay-open applet has no window),
-- which reads as "the app is broken". Re-show the dashboard instead — and if the server
-- died under us, start a fresh one (it opens the browser itself).
on reopen
	if serverIsUp() then
		do shell script "/usr/bin/open " & quoted form of dashURL
	else
		run
	end if
end reopen

-- Stay-open applets must return from `on idle`; we don't need periodic work, so idle
-- rarely (once an hour). The point of staying open is purely to keep `on quit` reachable.
on idle
	return 3600
end idle

on quit
	-- SIGTERM the server we started, give it a moment to shut down cleanly, then sweep the
	-- port with -9 as a backstop (covers a stale server we didn't start, or a wedged one).
	if serverPID is not "" then
		do shell script "/bin/kill " & serverPID & " 2>/dev/null; /bin/sleep 1; true"
	end if
	do shell script portSweepCmd()
	continue quit
end quit
