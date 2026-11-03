# Free X Following Watch for Discord

Choose several X accounts. When a watched account starts following someone new, receive a Discord notification identifying **both accounts**:

> **@elon followed @tesla**
>
> Tesla (@tesla) — https://x.com/tesla
>
> Watching: @elon (numeric account ID)
>
> Followed account ID and detection timestamp

This reads the watched accounts' **following lists**, not their incoming followers. It never follows, unfollows, posts, or sends DMs on X. The first successful check establishes a baseline instead of announcing every existing relationship.

**Use the exact usernames:** `@elon` and `@elonmusk` are different accounts. To watch Elon Musk, use `elonmusk`.

## No-payment approach and its trade-offs

- **X data:** free, open-source `twifork` using browser cookies from **an X account you own**. No X developer account, paid API token, Premium subscription, paid proxy, or hosted scraper is required by this script. There is no fallback to a paid service.
- **Hosting:** standard `ubuntu-latest` GitHub Actions runners in a **public repository**. Both supplied workflows deliberately skip private repositories to avoid potential private-runner billing. They do not upload artifacts or create Actions dependency caches.
- **Notifications:** Discord's built-in channel webhook; no separately hosted bot.
- **Not a guarantee of uninterrupted service:** X may block GitHub runner IPs, invalidate cookies, rate-limit the session, or change its web endpoints. This is unofficial, best-effort polling, not a supported X integration.
- **Account risk:** X's automation rules prohibit non-API website automation and warn that it can result in permanent account suspension. Do not use an account you cannot afford to lose. The script stops on rate limits or login problems; it does not rotate accounts, solve challenges, or purchase access.

Research references, checked November 3, 2026:

- [Twifork source and limitations](https://github.com/PawiX25/twifork): supports `get_user_following`; cookie access is required, and X-side blocks are outside the library's control. Version 2.4.0 is pinned and its actual pagination/parser are tested offline.
- [X automation rules](https://help.x.com/en/rules-and-policies/x-automation): website automation and suspension risk.
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions): standard public-repository runners are free; private repositories have shared allowances and potential billing.
- [Discord webhooks](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks): built-in channel notifications.
- [Official X pricing](https://docs.x.com/x-api/getting-started/pricing): official following/follower reads are paid, so this script does not use them.

## Setup

### 1. Use a public repository

Merge this pull request into the default branch. The repository was **private** when this version was prepared; its visibility has **not** been changed automatically.

To use this repository, review its contents and history, then change visibility through **Settings → General → Danger Zone → Change repository visibility**. Alternatively, put this project in a separate public repository. Do not expose any unrelated private files or credentials.

Your watch list, saved account IDs, queued Discord cards, and Actions logs will be public. Cookie and webhook values belong only in GitHub Secrets. Limit write access to trusted people: anyone able to alter a secret-bearing workflow could exfiltrate its secrets.

### 2. Add your own X cookies

The assistant does not supply or operate an X account for you. Use a session from an account you own and accept the automation risks for.

1. Log into **https://x.com** in your desktop browser.
2. Open developer tools (`F12`). In Chrome/Edge: **Application → Storage → Cookies → https://x.com**. In Firefox: **Storage → Cookies**.
3. Copy the values named **`auth_token`** and **`ct0`**.
4. In GitHub: **Settings → Secrets and variables → Actions → New repository secret**, create:

| Repository secret | Value |
| --- | --- |
| `X_AUTH_TOKEN` | The `auth_token` cookie value |
| `X_CT0` | The `ct0` cookie value |
| `DISCORD_WEBHOOK_URL` | The webhook URL created in step 3 |

Treat cookies like a password. Do not paste them into chat, issues, source files, screenshots, or logs. Logging out of X may invalidate the session; cookies can also expire independently. Refresh the secrets if login fails. If exposed, revoke the X session and replace the secrets.

Optional: instead of the two separate X secrets, set `X_COOKIES` to a JSON object containing `auth_token` and `ct0`, or a browser-export JSON array of `name`/`value` objects. If present, it takes precedence. Only the two required cookies are loaded; cookies are never saved to the state file.

**The old draft's `X_BEARER_TOKEN` is not used. No paid API key is needed.**

### 3. Create the Discord webhook

In the Discord channel: **Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL**. You need permission to manage webhooks. Store the URL in the GitHub secret `DISCORD_WEBHOOK_URL`.

Use the normal webhook URL, without adding `/github`. This script sends Discord messages directly.

### 4. Choose which accounts to watch

Edit `config.yml`:

```yaml
watch_mode: following
accounts:
  - elonmusk
  - nasa
  - SpaceX

page_size: 100
max_pages_per_run: 20
max_notifications_per_run: 50
announce_new_watch: true
```

Each account gets an independent baseline. If both `@elonmusk` and `@nasa` follow `@tesla`, you receive two separate notifications identifying who followed it.

Alternatively, create a GitHub Actions **repository variable** named `WATCH_ACCOUNTS` with a value such as `elonmusk,nasa,SpaceX`. This overrides the account list in the file. It is a variable, not a secret.

Per-account options in `config.yml`:

```yaml
accounts:
  - elonmusk
  - username: nasa
    enabled: false                   # pause this account
  - username: SpaceX
    webhook_env: DISCORD_WEBHOOK_SPACEX
```

For a separate Discord channel, add the named webhook secret and map it in the workflow's `env` block:

```yaml
DISCORD_WEBHOOK_SPACEX: ${{ secrets.DISCORD_WEBHOOK_SPACEX }}
```

Per-account options are not used when `WATCH_ACCOUNTS` overrides the file.

### 5. Install the workflow

This integration cannot publish `.github/workflows/` files because its GitHub App lacks workflows permission. Ready-to-use files are in **`workflow-templates/`**; the repository owner must install them once:

1. Copy the contents of `workflow-templates/follower-watch.yml`.
2. On GitHub, choose **Add file → Create new file**.
3. Name it **`.github/workflows/follower-watch.yml`**, paste, and commit on the default branch.
4. Optionally install `workflow-templates/tests.yml` as `.github/workflows/tests.yml` in the same way.

Merging the templates alone does **not** enable scheduled runs. If you move the templates rather than copy them, update the workflow-path assertion in the local tests too.

Allow state writes under **Settings → Actions → General → Workflow permissions → Read and write permissions**. Branch rules must permit the workflow's state commits. Use a dedicated automation repository rather than weakening protections on an unrelated project.

### 6. Run it

Open **Actions → X follow watch → Run workflow**, using the default branch.

- First successful run: a **Now watching @account** confirmation for each configured account; existing follows are not announced.
- Subsequent runs: messages such as **@elon followed @tesla** when an ID appears in that account's following list.
- Scheduled checks: minutes **7 and 37** of each hour, approximately every 30 minutes.
- Manual `dry_run`: preview Discord payloads in the log without posting or writing state. It still contacts X using your cookies. On a new watch it previews the baseline confirmation, not every existing follow.

If the job is skipped, check repository visibility and branch selection. GitHub can delay or drop scheduled runs and may disable public-repository schedules after 60 days without activity. Check the Actions page periodically.

## Reliability and limits

- Reads every visible page, including buffered library results. Does not assume follows are chronologically ordered or stop at the first known account.
- Compares stable numeric IDs, so reordering or changing a followed account's handle alone does not cause a new alert. Update configuration if a watched account changes its handle; its numeric-ID baseline is retained.
- Failed scans, malformed timeline responses, repeated pages, and exhausted page limits leave the previous snapshot intact. A suspicious empty list with a nonzero profile count is also rejected. X can still omit records or change a list during pagination, so complete real-world accuracy cannot be guaranteed.
- Failed Discord deliveries remain queued, and overflow is delivered over later runs. Local state is checkpointed after each successful message and GitHub commits state at the end of the run, including after a handled failure.
- Delivery is **at least once**, not exactly once: a crash after Discord accepts a message, a hard timeout, or a failed Git push can cause duplicate delivery. Successful webhooks and Git commits are not one atomic transaction.
- Follows reversed entirely between checks cannot be detected. An observed unfollow followed by a later observed refollow produces another alert. Visibility changes can also look like additions. The timestamp is **detection time**, not the exact time the account followed someone.
- Baselines and pending cards are stored in `state/followers_state.json`. Do not reset this file for routine testing. Adding an account starts a baseline; re-adding a previous account resumes its saved baseline. Legacy incomplete state is re-baselined once.
- Public watched accounts only. Protected watched accounts are deliberately rejected because snapshots are committed publicly. Only data visible to your authenticated session can be read.
- Each watched account requires a profile lookup and all visible list pages; large lists or many accounts can hit X limits. Start with a small list. There is a one-second pause between pages and three seconds between accounts; these do not guarantee protection from rate limits or suspension.
- All Discord mentions are disabled. A bio containing `@everyone` cannot ping your server.

## Settings reference

| Key | Default | Purpose |
| --- | --- | --- |
| `watch_mode` | `following` | Your requested behavior: watched account follows someone. Optional `followers` reverses direction. |
| `page_size` | `100` | Requested users per library page, 1–100; X may return different quantities. |
| `max_pages_per_run` | `20` | Page budget per account, including one final empty page. Incomplete scans fail safely. |
| `max_notifications_per_run` | `50` | Delivery limit per account per run; remainder stays queued. |
| `announce_new_watch` | `true` | Send first-run confirmation. |

Avoid `followers` mode for celebrities: incoming follower lists may be enormous. Leave `watch_mode: following` for the behavior requested here.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Missing cookie configuration | Add `X_AUTH_TOKEN` and `X_CT0`, or valid `X_COOKIES`, to repository Secrets. |
| X login needs attention | Sign into your own X account manually, resolve any account challenge, then refresh cookie secrets. |
| X rate limit | The run stops; wait for a later run and reduce the watch list or polling frequency. |
| X refused access / parser failed | Check the session, exact username, account visibility, and twifork's issue tracker. Runner IP blocks may make GitHub hosting unusable; no paid fallback is included. |
| Full list exceeds page budget | Increase `max_pages_per_run` cautiously, or watch fewer/smaller accounts. No new baseline is saved until the scan finishes. |
| Discord HTTP 401/403/404 | Replace the webhook secret or check channel/webhook permissions. |
| State push fails | Check Actions write permissions and branch rules. Duplicates are possible until fixed. |
| Invalid state | Restore the state file from Git history; corruption is not silently reset. |
| Workflow absent | Install the template under `.github/workflows/` on the default branch and enable Actions. |
| Job skipped | Use a public repository and select the default branch. |

Failures appear in Actions logs. Configure GitHub Actions failure notifications for email alerts if desired. The script does not send separate Discord outage notifications. No live X/Discord test has been performed without your credentials; passing offline tests does not prove X will accept the runner's requests.

## Local tests and execution

Python 3.10+; supplied workflows use 3.13:

```bash
pip install -r requirements-dev.txt
pytest -q

# Set X_AUTH_TOKEN, X_CT0 and DISCORD_WEBHOOK_URL in your environment.
DRY_RUN=1 python follower_watch.py
python follower_watch.py
```

Do not put real cookie values into shell commands saved in shared history. Optional environment variables: `CONFIG_PATH`, `STATE_PATH`, `WATCH_ACCOUNTS`, `DRY_RUN`, `X_COOKIES`.

The offline suite covers your multi-account example, independent state, unchanged scans, renames, refollows, real twifork pagination/parser fixtures, malformed responses, login/rate-limit shutdown, queue retries, dry-run isolation, and workflow billing guards.
