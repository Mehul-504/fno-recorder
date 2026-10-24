# fno-recorder

Records the NSE option chain for NIFTY and BANKNIFTY every minute of market hours
(09:15-15:30 IST, weekdays) on GitHub Actions, plus the day's 1-minute index candles,
NSE's F&O bhavcopy and participant-wise OI in the evening. Built to collect real intraday
option prices for backtesting. It never places orders.

Recorded data is encrypted (AES-256) before it is committed to `enc/`; the key lives only
on the owner's machine and in this repo's Actions secrets. NSE data is not republished.

- `recorder.py` - the recorder (`session`, `evening`, `once`)
- `save.sh` - encrypt `data/` into `enc/`, commit, push
- `decrypt.sh` - decrypt `enc/` locally with the key
- `setup_secrets.sh` - run once locally to create the key and set the Actions secrets
- `.github/workflows/record.yml` - the schedule
