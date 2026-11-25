-- LuCI controller for the multi-dial dashboard.
-- Embeds the Vite/React SPA inside a LuCI page (so LuCI login gates access =
-- multi-user auth) and proxies the SPA's API calls, same-origin, to the local
-- Python daemon on 127.0.0.1:23334. Lua controllers work on op's LuCI 26.x via
-- luci-compat (verified with luci-app-easytier).
module("luci.controller.multidial", package.seeall)

local DAEMON = "http://127.0.0.1:23334"
-- A safe URL path segment: account names (a-z0-9_), device names (dial0), and
-- userid-ish tokens only. Anything with shell metacharacters is rejected, so
-- validated segments are safe to interpolate into the curl command.
local SEG = "^[%w_%.@%-]+$"

function index()
	-- NOT a leaf: the child API routes must remain reachable (a leaf would
	-- swallow trailing path segments as template args).
	entry({"admin", "services", "multidial"},
		template("multidial/dashboard"), _("多拨管理"), 60)
	-- same-origin API proxy (auth-gated by the LuCI session)
	entry({"admin", "services", "multidial", "status"},  call("api_status")).leaf = true
	entry({"admin", "services", "multidial", "reauth"},  call("api_reauth")).leaf = true
	entry({"admin", "services", "multidial", "reload"},  call("api_reload")).leaf = true
	entry({"admin", "services", "multidial", "power"},   call("api_power")).leaf = true
	entry({"admin", "services", "multidial", "sqm"},     call("api_sqm")).leaf = true
	entry({"admin", "services", "multidial", "log"},     call("api_log")).leaf = true
	entry({"admin", "services", "multidial", "history"}, call("api_history")).leaf = true
	entry({"admin", "services", "multidial", "account"}, call("api_account")).leaf = true
	entry({"admin", "services", "multidial", "dial"},    call("api_dial")).leaf = true
	entry({"admin", "services", "multidial", "traffic"}, call("api_traffic")).leaf = true
	entry({"admin", "services", "multidial", "sticky"},  call("api_sticky")).leaf = true
end

local function http() return require "luci.http" end

local function reqmethod()
	return http().getenv("REQUEST_METHOD") or "GET"
end

-- Raw JSON request body (may contain a password) — read as-is, never parsed
-- into the command line.
local function read_body()
	local h = http()
	local b = h.content and h.content() or nil
	return b or ""
end

local function err_json(msg)
	local h = http()
	h.prepare_content("application/json")
	h.write('{"error":"' .. tostring(msg) .. '"}')
end

-- limit query param, clamped to a sane integer (or "" if absent/invalid).
local function limit_qs()
	local l = tonumber(http().formvalue("limit"))
	if l and l > 0 and l <= 2000 then
		return "?limit=" .. tostring(math.floor(l))
	end
	return ""
end

-- Forward (method, path[, body]) to the daemon via curl. The body, which may
-- carry a secret, goes through a temp file (--data-binary @file) so it never
-- appears on the command line or in `ps`. `path` is built only from fixed
-- literals + SEG-validated segments + integer limits, so the single-quoted url
-- has no injectable content.
local function proxy(method, path, body)
	local h = http()
	local fs = require "nixio.fs"
	local url = DAEMON .. path
	local cmd, tmp
	if body ~= nil and body ~= "" then
		-- every request is a fresh, unseeded Lua: math.random repeats, so two POSTs
		-- in one second shared a file and one curl lost its body. The pid is unique.
		tmp = string.format("/tmp/.multidial_req.%d.%d", require("nixio").getpid(), os.time())
		fs.writefile(tmp, body)
		cmd = string.format(
			"curl -s -m 8 -X %s -H 'Content-Type: application/json' --data-binary @%s '%s'",
			method, tmp, url)
	elseif method ~= "GET" then
		cmd = string.format("curl -s -m 8 -X %s '%s'", method, url)
	else
		cmd = string.format("curl -s -m 8 '%s'", url)
	end
	local p = io.popen(cmd)
	local resp = p and p:read("*a") or ""
	if p then p:close() end
	if tmp then fs.unlink(tmp) end
	if resp == nil or resp == "" then
		resp = '{"error":"daemon unreachable","dials":[],"accounts":[],' ..
			'"aggregate":{"total":0,"online":0}}'
	end
	h.prepare_content("application/json")
	h.write(resp)
end

function api_status() proxy("GET", "/api/status") end
function api_reauth() proxy("POST", "/api/reauth") end
function api_reload() proxy("POST", "/api/reload") end
function api_power()  proxy("POST", "/api/power", read_body()) end
function api_sqm()    proxy("GET", "/api/sqm") end
function api_log()    proxy("GET", "/api/log" .. limit_qs()) end
function api_history() proxy("GET", "/api/history" .. limit_qs()) end
function api_traffic() proxy("GET", "/api/traffic") end

-- GET /sticky -> settings + pins + health; POST /sticky -> partial update (JSON)
function api_sticky()
	if reqmethod() == "POST" then
		proxy("POST", "/api/sticky", read_body())
	else
		proxy("GET", "/api/sticky")
	end
end

-- POST /account            -> create/update (name is in the JSON body)
-- DELETE /account/<name>   -> delete
function api_account(name)
	local m = reqmethod()
	if m == "DELETE" then
		if not name or not name:match(SEG) then
			return err_json("invalid account name")
		end
		proxy("DELETE", "/api/account/" .. name)
	else
		proxy("POST", "/api/account", read_body())
	end
end

-- POST   /dial/<device>/<stop|start|mark|rotate|reauth>
-- DELETE /dial/<device>                                   -> decommission (delete one)
function api_dial(device, action)
	if not device or not device:match(SEG) then
		return err_json("invalid device")
	end
	if reqmethod() == "DELETE" and not action then
		return proxy("DELETE", "/api/dial/" .. device)
	end
	if action ~= "stop" and action ~= "start" and action ~= "mark"
		and action ~= "rotate" and action ~= "reauth" then
		return err_json("invalid action")
	end
	local body = (action == "mark") and read_body() or nil
	proxy("POST", "/api/dial/" .. device .. "/" .. action, body)
end
