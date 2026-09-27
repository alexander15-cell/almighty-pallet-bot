# Four Guys One Pallet — combined bot

One program and one Discord bot token now run Almighty's intake workflow and
the website publisher. **This ZIP does not turn anything on by itself.**

## How staff use it

1. Add photos and a description in **#data-entry**.
2. Review the item and finish the normal listing steps. List it on eBay yourself,
   then use **Confirm Listed**. The bot does not create an eBay listing for you.
3. The completed item gets a separate card in **#website_shop**.
4. Click **Enter price and eBay link**. Enter the selling price and that item's
   full eBay link. Check the photos, description, condition and quantity.
5. Click **Approve for website**. It is queued first; publication is confirmed
   only after the website accepts the update. Use **Show latest review** to see
   the latest saved status or recover an older card.
6. When it sells, click **Mark as Sold** on the latest #listed card. Website
   removal is queued automatically. `/website hold` similarly queues removal;
   use the hold card's **Resolved** button when ready, then approve it again.

The bot does not watch eBay sales automatically. Someone must mark the sale in
Discord. An offline website may delay updates; saved work is retried.

## Before you start — important for your current live website

**Do not overwrite your existing bots, database, photos or Publisher journal.**
Extract this ZIP into a NEW folder. It deliberately starts a separate inventory
namespace for new items. It does not reconstruct the partner's missing database,
adopt the 56 existing listings, or transfer old buttons to another application.
Those products stay on the website, and all old Discord posts remain unchanged.

The current Publisher must be stopped during the coordinated live handover.
Keep its files and private backups. Existing listings still need their current
Publisher or a separate reviewed migration to support future automated changes;
this package's new-item controls cannot sell, hold or edit those historical
products. Until that migration, update historical stock through the existing
process or website admin. Do not assume stopping the old watcher makes this one
responsible for its historical listings.

## Windows setup

1. Install **Python 3.12** for Windows, including the Python launcher. Extract
   the ZIP completely; do not run it from inside the ZIP or a synced/network drive.
2. In Discord, give **Four Guys One Pallet — Test** these permissions in the ten
   product channels already listed in `setup.example.json`: View Channel, Read
   Message History, Send Messages, Embed Links, Attach Files. #website_shop was
   already verified; the other nine still needed access at packaging time.
   Do not grant Administrator or access to DMs, general chat or finance channels.
3. Enable **Message Content Intent** for this application in Discord Developer
   Portal. Invite it with `bot` and `applications.commands` scopes. No second
   Publisher token is needed. Do not run another copy of the same intake bot.
4. Double-click **Setup.cmd**. It installs the tested dependencies and asks for:
   - Staff user IDs allowed to operate the workflow.
   - The IDs of Pallet Admin, Queue Review and Listing Management roles.
   - The existing website source ID and shared connection key.
   - The replacement Almighty bot token, entered privately without echoing it.
   Discord Settings → Advanced → Developer Mode enables **Copy ID** when you
   right-click a user or role. Staff must have the relevant configured role too.
5. Setup saves credentials only in **private\setup.json**, and new inventory,
   photos and queues in **private\state**. Never send the private folder, token,
   or website key through chat or put them in a ZIP/GitHub. Keep this folder
   private to the Windows account running the bot.
6. Double-click **Check.cmd**. This checks local setup without any network calls.
   If Setup saved invalid details, correct `private\setup.json` in Notepad and
   run `powershell -NoProfile -File .\Manage.ps1 -Action Initialize` once.
   Never initialize over existing state or delete it to get past an error.
7. **Preview.cmd** connects to Discord and enables real intake/review posts,
   but sends NOTHING to the website. It also registers this bot's commands in
   the configured server. It does not change the partner's application commands.
   Use only when you are ready for staff to start working in the real channels.

## Website connection and live handover

The public website is **https://4guys1palletoverstock.com**. A teammate on another
computer uses that HTTPS address, not localhost and not your computer's IP.
Your website computer and its Cloudflare tunnel must stay running and awake.

On the website host, the existing private environment settings are:

| Website setting | Bot's private/setup.json field |
| --- | --- |
| DISCORD_SYNC_SOURCE_ID | sourceId |
| DISCORD_SYNC_GUILD_ID | guildId |
| DISCORD_SYNC_SECRET | websiteSecret |
| DISCORD_SYNC_ENABLED=true | publishing additionally requires the switches below |

Copy the existing values privately; **do not generate a new source ID, reset a
journal or change the website key just to start this ZIP**. Existing products
have stable ownership. This package uses new item IDs starting at 1,000,000,000
and FGNEW- SKUs so it does not reuse the historical item numbers. Its separate
journal must be retained for every restart and update. Website receiving support
for eBay item links and explicit quantity is already present in this project's
website; no website files or secret values are bundled here.

For the planned handover, first back up the website and both bot state folders,
stop the old intake/Publisher processes on every host, and confirm only this
computer will run the new sender. Preserve any pending old work and the historical
stock-update plan above. Stop Preview with **Ctrl+C**, then edit these two fields
in the private setup file:

```json
"publishEnabled": true,
"cutoverConfirmed": true
```

Double-click **Start.cmd**. Test one real approved item, confirm its website
photos/price/eBay button, and verify its sold/hold removal and restart recovery
before relying on the handover. No such live cutover was performed when this ZIP
was built. Leave the console open; **Ctrl+C** stops it without deleting saved work.
The package does not install a background service or automatic Windows startup.

## What is included and what is intentionally off

- Almighty intake, queue review, listing cards, eBay/FB CSV workflow and sold/
  shipped controls; plus the website review form, durable publishing and retries.
- Old messages are preserved; stale buttons cannot advance an older version.
  Shop review cards do not replace the original workflow card.
- New physical intake records represent one tracked unit each. Quantity prefixes
  create separate item records. The old multi-quantity catalog is not reimported.
- Automatic AI review, direct eBay API, Cloudflare R2 uploads, QuickBooks, finance,
  shipping integrations, server setup and deletion/purge tools are disabled in
  the guarded launcher. No paid AI/API subscription is required. Optional SDKs
  remain installed only because upstream modules import them.
- With R2 disabled, eBay CSV photos do not get new public hosting links. Upload
  photos on eBay manually. The website gets actual validated image bytes directly.
- This replaces the executable workflow, not Discord bot ownership. Old posts
  and their original buttons remain, but only newly created cards are managed.

## Trouble checks

- **Permission error:** Check the exact bot member in each of the ten channels;
  Publisher's permissions do not apply to the replacement bot.
- **Not on the website:** Check explicit approval, Start vs Preview, both live
  switches, the exact source/key match, and website/Cloudflare availability.
- **Bad eBay link:** Use a full `https://www.ebay.com/itm/ITEMNUMBER` link, not a
  shop/search/short link. Variation links are rejected rather than guessing.
- **Review changed:** Click Show latest review and approve the current details.
- **Connection-key or collision error:** Stop and investigate. Do not erase state
  or change IDs; that can lose ordering or create duplicate products.
- **New PC:** Stop this bot first. Move the complete private folder securely,
  including all databases, journals and photos; never run both copies. Upstream
  intake stores absolute photo paths, so retain the same full folder path or
  have those paths explicitly relocated and verified before starting. Merely
  copying databases to a differently named folder is not a complete migration.
- Back up the entire private folder while the bot is stopped. Keep a copy off
  this device. A backup/restore service is not installed by this ZIP.
