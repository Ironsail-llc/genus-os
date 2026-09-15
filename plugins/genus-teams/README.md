# genus-teams

Microsoft Teams as a Genus OS channel — outbound delivery, an authenticated
messaging endpoint, pairing, and interactive questions over Adaptive Cards.

This is the **reference channel plugin**: the first channel to ship outside the
engine, and the payload that proves the `genus.channels` seam carries a real
surface rather than a socket with nothing plugged into it.

```bash
genus plugin install genus-teams --sha256 <digest> --accept-review
genus channel add teams --app-id <application id>
genus config set channels.enabled teams
sudo systemctl restart robothor-engine
```

The install verdict is `review`, not `safe`. That is correct for a channel: it
reaches off the box (the Bot Framework REST API) and it contributes to a group
that runs without a tool call. Read the reasons the installer prints.

**Installing does not arm it.** A channel stays inert until named in
`ROBOTHOR_CHANNELS`, because a package able to become your delivery surface
merely by being installed could intercept every briefing.

**Somebody must message the bot before anything can be delivered to them.**
Teams cannot be addressed from an id: a proactive message needs the tenant's own
`serviceUrl` and a conversation id, and both arrive only on an inbound activity.
A delivery with no recorded reference fails loudly rather than posting somewhere
nobody chose.

Set-up, the Azure objects to create, the ingress decision (on a deployment
behind Cloudflare Access, the messaging endpoint needs a bypass for exactly one
path), what the endpoint refuses, and what gets recorded:
[`docs/channels/teams.md`](../../docs/channels/teams.md).

## Tests

Offline, and nothing contacts Microsoft: the Bot Framework's published keys are
served from a fixture and every token is minted in-process from a key pair the
suite generates.

```bash
PYTHONPATH=<repo>:<repo>/plugins/genus-teams pytest plugins/genus-teams
```
