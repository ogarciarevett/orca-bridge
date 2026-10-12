# Run the bridge for a remote client

The remote interface serves the existing four tools and policy. All examples
use placeholders. Replace them locally, outside Git. A token grants access to
the configured coordinator mailbox; use a separate personal service and keep
the Bridge target allowlist narrow.

## 1. Install and configure

Run `uv tool install .` from the checkout. Configure the Bridge identity,
targets and private state as described in the README. Create private `http.json`
in `$XDG_CONFIG_HOME/orca-bridge` or `~/.config/orca-bridge`, with mode 600:

```json
{"port": 8787, "allowed_hosts": ["<host>.ts.net"],
 "requests_per_minute": 60, "max_request_bytes": 65536}
```

Use the exact external Host header in the local allowlist. Unknown settings,
wildcard hosts and insecure files fail closed. Loopback hosts on the configured
port are added automatically. Port bounds are 1024 to 65535; request limits are
1 to 600 per minute; body limits are 1024 to 1048576 bytes; at most 20 explicit
hosts are accepted. Rate windows are per server process and reset on restart.

## 2. Create and store a token

Generate at least 32 URL-safe random characters on a trusted device, for
example with Python's `secrets.token_urlsafe(32)`. Save the value in your password
manager and the operating system keychain using their secure input UI.
Configure your trusted `with-secrets` wrapper to inject the keychain value under
`ORCA_BRIDGE_HTTP_TOKEN`. Never put the value in shell arguments, shell history,
configuration templates or Git. The launcher requires the variable, removes
it after reading it, and uses constant-time credential comparison.

Run through the configured wrapper:

```text
/ABSOLUTE/PATH/with-secrets ORCA_BRIDGE_HTTP_TOKEN -- /ABSOLUTE/PATH/orca-bridge-http
```

The bridge listens only on loopback. It offers no host flag and does not set up
a tunnel or configure the system keychain for you.

## 3. Optional macOS service

Copy [the launchd example](../examples/launchd/orca-bridge-http.plist.example)
to a private local plist. Replace the executable paths and label locally. The
wrapper must work without an interactive shell and must not log the secret.
Keep the private config readable by the service's account. The example uses
the default XDG fallback directories; add local XDG environment settings only
when needed. A launch agent's PATH may be limited, so configure the private
Bridge `orca_command` as its expanded absolute executable path.

After reviewing the local plist, the owner can load it:

```sh
launchctl bootstrap "gui/$(id -u)" '/ABSOLUTE/PATH/<label>.plist'
```

The example starts at login, restarts after exits and discards stdout/stderr.
Inspect the private HTTP audit log for request results.

## 4. Expose HTTPS and configure the remote client

Use a trusted tunnel on a personal tailnet. The owner can enable Funnel with:

```sh
tailscale funnel --bg 8787
tailscale funnel status
```

Funnel publishes the local service to the internet over HTTPS; it does not
authenticate callers for this bridge. Keep the token checks enabled and disable
any proxy access logging that would capture a secret path. Validate the remote
hostname against the private allowlist before connecting.
Verify the installed Tailscale CLI's Funnel help before applying these commands.

Use either bearer authentication at the MCP route or the secret URL path:

```text
https://<host>.ts.net/mcp
Authorization: Bearer <token>

https://<host>.ts.net/<token>/mcp
```

In a Remote HTTPS MCP dialog with only a URL field, use the second form. Treat
that URL like a password. Both forms serve the same tools, mailbox and policy.
GET, POST and DELETE are forwarded after authentication; the MCP SDK determines
which operations are supported in stateless mode. Other paths are refused.

## 5. Verify safely

First verify that an unauthenticated request returns 401, using no secret:

```sh
curl -i 'https://<host>.ts.net/mcp'
```

For authenticated testing, create a mode 600 temporary curl config with your
password manager or secure editor. It should contain the endpoint URL and a
bearer header, or the secret path URL. Never pass the token directly in curl's
argv. A placeholder bearer config is:

```text
url = "https://<host>.ts.net/mcp"
header = "Authorization: Bearer <token>"
header = "Content-Type: application/json"
header = "Accept: application/json, text/event-stream"
```

Then send a nonsecret initialization request from a local JSON file:

```sh
curl --config '/ABSOLUTE/PATH/private-curl.conf' --data-binary @'/ABSOLUTE/PATH/initialize.json'
```

```json
{"jsonrpc":"2.0","id":1,"method":"initialize",
 "params":{"protocolVersion":"2025-03-26","capabilities":{},
 "clientInfo":{"name":"bridge-check","version":"1"}}}
```

Delete the temporary curl config after the check. Use a real MCP client to list
exactly four tools and perform a status call before enabling requests. The
test suite verifies both auth styles using a real client on disposable loopback
servers; external TLS, tunnel setup and client UI acceptance are operator checks.

## Logs and revocation

Logs contain UTC time, HTTP method, route category, response status, byte
counts, elapsed milliseconds, normalized client IP, auth style and known RPC
method/tool names. Forwarded client IPs are proxy-supplied metadata, not an
authentication factor. Unknown RPC names are logged as `other`. Bodies,
arguments, raw paths, headers and tokens are excluded. Protect the log and its
one rotated copy; neither should be published.

To rotate credentials, stop exposure, replace the keychain/password-manager
value, restart the service, and update the remote client. Old tokens stop working
only after the server restarts. All consequential requests still require the
coordinator to obtain owner approval.

## KILL SWITCH

Remove public exposure and stop the launch agent:

```sh
tailscale funnel reset
launchctl bootout "gui/$(id -u)/<label>"
```

Funnel reset clears this device's Funnel configuration, including other shares.
For a scoped shutdown of the example, use the original flags and `off`:

```sh
tailscale funnel --bg 8787 off
```

Stopping the launch agent prevents KeepAlive from restarting it. For a manually
launched service, also stop its foreground process. Revoke the remote client's
server entry and replace the stored token before re-enabling exposure.
