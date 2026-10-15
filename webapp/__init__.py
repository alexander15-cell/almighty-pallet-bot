"""
The web dashboard - a second way into the same bot, alongside Discord.

It runs inside the bot's own process (cogs/web_dashboard.py starts it), on
the bot's own database, and every action it offers calls the same code the
Discord buttons do - so an item approved on the website moves in Discord
too, and a sale logged in Discord shows up on the website. People sign in
with Discord, and their Discord roles decide what they can do here, exactly
as in Discord.

Off unless WEB_DASHBOARD_ENABLED=true. Meant to be reached through a
Cloudflare Tunnel (https, no open router ports) - see README's "Web
dashboard" section.

  auth.py      - Sign in with Discord, sessions, role checks
  layout.py    - HTML page shell and small formatting helpers
  server.py    - the aiohttp app, middleware (auth, CSRF, security headers)
  inventory.py - inventory browser + item pages + photos
  finance.py   - pallet profit, sales, payouts and the pending eBay cards
  pipeline.py  - phone intake, Queue Review, awaiting listing
"""
