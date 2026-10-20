# Running Taraba Central Billing and Collections on a VPS

One server runs everything: the public site, the payment API, the staff console
(behind sign-in), the background worker and the database. HTTPS certificates are
obtained and renewed automatically.

```
Internet ──► Caddy (HTTPS, ports 80/443) ──► app (FastAPI) ──► PostgreSQL
                                              ▲
Monnify ── webhook ───────────────────────────┘     worker ──► Monnify API
```

| Address | What it is |
|---|---|
| `https://taraba.govpayhub.com/` | Public page: find **one** notice by notice number or TTIN, view it, pay it, download receipts. Contains no registry. |
| `https://taraba.govpayhub.com/staff/` | Staff console (dashboard, registry, reports), only after an officer signs in. |
| `https://taraba.govpayhub.com/api/monnify/webhook` | Where Monnify reports payments. |

## 1. The server

- Ubuntu 22.04 or 24.04, 2 vCPU, 4 GB RAM, 40 GB disk is plenty.
- A fixed public IP address.
- Install Docker (official convenience script, run as root):
  ```sh
  curl -fsSL https://get.docker.com | sh
  ```
- Firewall: allow only SSH, HTTP and HTTPS:
  ```sh
  ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw enable
  ```
- Create a deploy user that can run Docker, and log in with an SSH key (not a password):
  ```sh
  adduser deploy && usermod -aG docker deploy
  mkdir -p /opt/tirs && chown deploy /opt/tirs
  ```

## 2. DNS

Point `taraba.govpayhub.com` at the VPS: an **A record** with the VPS's IP address
(and remove the record that points at Netlify). Until DNS points at the VPS, Caddy
cannot obtain the HTTPS certificate.

## 3. Settings (on the VPS only)

```sh
cd /opt/tirs/deploy          # created by the first publish, or copy the deploy folder by hand
cp .env.example .env
nano .env                    # fill in every value; generate secrets with: openssl rand -hex 32
chmod 600 .env
```

Never put real keys in chat, email or git. `.env` is ignored by git.

## 4. Publish (from the office PC)

```powershell
.\deploy\publish.ps1 -Server deploy@<vps-ip>
```

This builds from the spreadsheet, copies the server code, the public page, the staff
console and the data export to `/opt/tirs`, rebuilds the containers and loads the
notices. Run it again whenever the spreadsheet or the code changes. If a new
spreadsheet would renumber or drop a notice that already has payments, the load is
refused and nothing changes.

## 5. Staff accounts

Each officer gets their own account (sign-ins and console views are logged by name):

```sh
cd /opt/tirs/deploy
docker compose exec app python -m app.cli create-staff jdoe "Jane Doe"    # prompts for a password (12+ chars)
docker compose exec app python -m app.cli list-staff
docker compose exec app python -m app.cli set-password jdoe
docker compose exec app python -m app.cli deactivate-staff jdoe           # when someone leaves
```

Five wrong passwords lock that username (and that address) for 15 minutes.

## 6. Backups

The database is the payment ledger. Back it up daily and copy the file off the VPS:

```sh
# /etc/cron.d/tirs-backup
15 2 * * * deploy cd /opt/tirs/deploy && docker compose exec -T db pg_dump -U tirs tirs | gzip > /opt/tirs/backups/tirs-$(date +\%F).sql.gz && find /opt/tirs/backups -mtime +30 -delete
```

(`mkdir -p /opt/tirs/backups` first.) Restore test: `gunzip -c file.sql.gz | docker compose exec -T db psql -U tirs tirs`.

## 7. Monnify: sandbox first

1. In the Monnify **sandbox** dashboard, copy the API Key, Secret Key and Contract Code
   into `.env` (`MONNIFY_ENV=sandbox`).
2. Register the webhook URL `https://taraba.govpayhub.com/api/monnify/webhook`
   (Developers > Webhook URLs > Transaction Completion).
3. Test against a sandbox dataset with balances to pay (sandbox only):
   ```sh
   docker compose exec app python -m app.cli sync --sandbox-unpaid
   ```
   and set `PAYMENTS_ENABLED=true` in `.env`, then `docker compose up -d`.
4. Pay a notice with Monnify's test cards (from Monnify's docs), for example
   `4111 1111 1111 1111`, CVV `123`, PIN `1234`; check the notice turns Settled, the
   receipt downloads and its QR code verifies.
5. Before going live, reload the real data: `docker compose exec app python -m app.cli sync`.

## 8. Go-live gate

Leave `PAYMENTS_ENABLED=false` until **all** of these are true:

- [ ] A dataset with real outstanding balances (in the 2025 file every account is already paid).
- [ ] Live Monnify keys and contract code in `.env`, `MONNIFY_ENV=live`; settlement set to
      the State's bank account (Monnify defaults to its wallet: Settings > Contract Setup).
- [ ] Monnify has confirmed which key signs production webhooks (`monnify-signature`).
      If it is not the Secret Key, set `MONNIFY_WEBHOOK_SECRET`.
- [ ] Policy decided: who pays Monnify's fee; whether part-payments are allowed
      (Monnify rejects them by default).
- [ ] A live ₦100-level test payment made, receipted, and reconciled the next morning
      (`recon_runs` table, or `docker compose exec app python -m app.cli recon YYYY-MM-DD`).

## 9. Retire the Netlify site

Once the VPS serves `taraba.govpayhub.com`, the old Netlify site still answers at
`taraba-central-billing.netlify.app` and still contains the **whole registry**.
Delete it in Netlify (Site configuration > General > Delete site), or at minimum
deploy an empty page there.

## Day-to-day

```sh
docker compose ps                         # what is running
docker compose logs -f app worker         # live logs
docker compose exec db psql -U tirs tirs  # database shell (read-only use, please)
```

## Testing on a PC (no VPS, no Monnify)

`deploy/docker-compose.local.yml` runs everything locally with a stand-in for
Monnify (`server/tests/fake_monnify.py`) so the whole flow can be tried without keys:

```sh
python build.py
docker compose -f deploy/docker-compose.local.yml up -d --build
docker compose -f deploy/docker-compose.local.yml exec app python -m app.cli sync --sandbox-unpaid
# http://localhost:8000
docker compose -f deploy/docker-compose.local.yml --profile tests run --rm tests   # automated tests
```
