# Caddy in front of gunicorn (plan, Task 14).
# - TLS: a publicly trusted Let's Encrypt certificate for the bare IP (the short-lived
#   profile, the only one LE issues for IP addresses). Never an internal CA.
# - HTTP redirects to HTTPS (Caddy's automatic redirect).
# - /admin is never proxied: it is a 404 here, and reachable only over the SSH tunnel to
#   gunicorn's loopback port (Django also refuses non-loopback, LoopbackAdminMiddleware).
# - /static/ from the collected store path, `Cache-Control: no-cache` (the service worker
#   versions its cache by content hash).
# - /sw.js goes to Django, uncached.
# - Per-client-IP rate limits on the pages and the API (caddy-ratelimit).
# No HSTS while the site is on an IP: browsers ignore it there.
{
  pkgs,
  site,
  agendaApp,
  ...
}:
{
  services.caddy = {
    enable = true;
    package = pkgs.caddy.withPlugins {
      plugins = [ "github.com/mholt/caddy-ratelimit@v0.1.0" ];
      hash = "sha256-nYE5/88oJ9Od6ohlzfr3uybPlNnrqnPccmIqyn1Kz7E=";
    };
    # The admin API on a Unix socket only Caddy can use, not on localhost:2019 where any
    # local user (the app's included) could reconfigure it. `caddy reload` reads it from here.
    globalConfig = ''
      admin unix//run/caddy/admin.sock|0600
      grace_period 10s
      order rate_limit before reverse_proxy
      servers {
        # The firewall opens TCP 443 only: no HTTP/3 to advertise.
        protocols h1 h2
      }
    '';
    virtualHosts.${site.siteHost} = {
      extraConfig = ''
        tls {
          issuer acme {
            dir https://acme-v02.api.letsencrypt.org/directory
            profile shortlived
          }
        }

        encode zstd gzip

        @admin path /admin /admin/*
        handle @admin {
          respond 404
        }

        handle_path /static/* {
          root * ${agendaApp.static}
          header Cache-Control "no-cache"
          file_server
        }

        handle /api/* {
          rate_limit {
            zone api {
              key {remote_host}
              events 240
              window 1m
            }
          }
          reverse_proxy unix//run/agenda-web/gunicorn.sock
        }

        handle {
          rate_limit {
            zone pages {
              key {remote_host}
              events 120
              window 1m
            }
          }
          header /sw.js Cache-Control "no-cache"
          reverse_proxy unix//run/agenda-web/gunicorn.sock
        }
      '';
    };
  };

  systemd.services.caddy.serviceConfig.RuntimeDirectory = "caddy";
  # Caddy reads the gunicorn socket (group caddy, 0660).
  systemd.services.caddy.after = [ "agenda-web.socket" ];
}
