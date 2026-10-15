# Security

cooldownd can type into your coding agents, restart services and sleep your machines, so a hole matters.

**Please don't open a public issue for a vulnerability.** Report it privately through GitHub: the repository's **Security** tab → **Report a vulnerability**. Say what an attacker needs (on the tailnet? a listed login? at the kiosk?) and what they get.

What's in scope: anything that lets a request through without a listed Tailscale login or the hub token, a cross-site request that triggers an action, a way out of the fixed command list, or a secret or personal file served by the panel.

What's by design: the kiosk's `SCREEN_PORT` trusts whoever is at the screen; anyone in `allowed_users`, and any panel holding the hub token, has full control; and every process on a panel's own machine is trusted, because it can reach `127.0.0.1` and send the login header `tailscale serve` would add.
