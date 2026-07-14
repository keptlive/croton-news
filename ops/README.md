# Ops artifacts (versioned snapshots)

Live locations on the croton VPS — after editing here, deploy with cp +
reload. Crontab: `crontab ops/crontab-croton.txt` (WARNING: whole-user
crontab, includes other sites albion/cranberry/clay).

| File | Live location |
|---|---|
| nginx-croton.news.conf | /etc/nginx/sites-enabled/croton.news (then: nginx -t && systemctl reload nginx) |
| croton-news.service | /etc/systemd/system/croton-news.service (then: systemctl daemon-reload) |
| logrotate-croton-news | /etc/logrotate.d/croton-news |
| crontab-croton.txt | root crontab on croton VPS |
| ../wireclaw/crontab-wireclaw.txt | root crontab on WireClaw box |

Secrets are NOT here: /opt/croton-news/secrets.env (600), .env (600),
rag/.env (600).
