# FulCrop Merchant Center feed

Every day a GitHub Action reads the products on fulcropsciences.com (WooCommerce Store API)
and rebuilds `merchant_feed.tsv`. Google Merchant Center downloads it from:

    https://raw.githubusercontent.com/paulfulcrop/fulcrop-feed/main/merchant_feed.tsv

- Run it by hand: Actions tab → "Build Merchant Center feed" → Run workflow.
- Change titles, categories or exclusions: edit the settings at the top of `build_merchant_feed.py`.
- `last_run.txt` is written on every run so the repo stays active (GitHub pauses
  scheduled workflows in repos with no activity for 60 days).
