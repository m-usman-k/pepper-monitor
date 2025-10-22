# Pepper Monitor Discord Bot

Monitor pepper.pl subpages and instantly post new deals to Discord channels using slash commands. Embeds use Discord blurple.

## Quick start

1. **Install Python 3.10+**
2. **Create venv and install deps**
   - Windows (PowerShell):
     ```powershell
     python -m venv .venv
     .venv\Scripts\Activate.ps1
     pip install -r requirements.txt
     ```
3. **Edit `config.py`**
   - Set `BOT_TOKEN`
   - Set `OWNER_ID` (your numeric Discord user ID)
   - Optionally adjust intervals and proxy rotation settings
4. **Optional: add proxies**
   - Put lines into `proxies.txt` like `ip:port:user:pass` or `ip:port`
   - Ensure `USE_PROXIES = True` in `config.py` if you want to use them
5. **Run the bot**
   ```bash
   python bot.py
   ```

## Commands (slash)

- **/help**
- **/alert add [name] [link]**
- **/alert remove [name]**
- **/alert list**

Only the `OWNER_ID` user can use these commands.

## Notes

- Commands are synced globally on startup and may take up to ~1 hour to appear if it’s the first run. Subsequent updates are usually faster.
- Each monitor runs independently; the bot can handle 10+ links concurrently.
- Latest deal detection uses resilient HTML parsing; fields like code/price may be missing if not present on the card.

## Files

- `bot.py` – Discord client and slash commands
- `monitor.py` – per-link async monitor manager and embed sending
- `scraper.py` – HTML fetch and parse for latest deal
- `proxies.py` – proxy rotation from `proxies.txt`
- `storage.py` – JSON persistence of monitors and seen deals in `data/`
- `config.py` – all configuration
- `requirements.txt` – dependencies
- `proxies.txt` – optional proxy list

## Troubleshooting
