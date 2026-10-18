#!/usr/bin/env python3
"""Lifecycle email ops: audience report, dry-run, scoped sweep, test send.

Imports the app (startup migrations run), so point DATABASE_URL at the
database you mean. Never prints email addresses.

  report                         who is in each audience now (ids + counts)
  sweep --dry-run                what the background sweeper would send now
  sweep --only-user-id N [...]   real send, limited to these ids (needs
                                 LIFECYCLE_EMAILS_ENABLED=1 + mail ready)
  test-send --user-id N --campaign C --to ADDR
                                 render user N's real email for campaign C
                                 and send it to ADDR only, "[TEST]" subject,
                                 nothing recorded against user N
  render --user-id N --campaign C --out DIR
                                 write subject/text/html to DIR, no sending
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('PODSKRIFT_DISABLE_WATCHDOG', '1')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('report')
    sw = sub.add_parser('sweep')
    sw.add_argument('--dry-run', action='store_true')
    sw.add_argument('--only-user-id', type=int, action='append')
    sw.add_argument('--no-rss', action='store_true',
                    help='skip the next-episode RSS lookup')
    ts = sub.add_parser('test-send')
    ts.add_argument('--user-id', type=int, required=True)
    ts.add_argument('--campaign', required=True)
    ts.add_argument('--to', required=True)
    rd = sub.add_parser('render')
    rd.add_argument('--user-id', type=int, required=True)
    rd.add_argument('--campaign', required=True)
    rd.add_argument('--out', required=True)
    args = p.parse_args(argv)

    import app as A
    import lifecycle_emails as L

    with A.app.app_context():
        if args.cmd == 'report':
            print(json.dumps(L.audience_report(), indent=2, default=str))
            return 0
        if args.cmd == 'sweep':
            if not args.dry_run and not args.only_user_id:
                print('refusing a real sweep without --only-user-id; the '
                      'background sweeper does full sweeps', file=sys.stderr)
                return 2
            res = L.sweep(dry_run=args.dry_run, only_user_ids=args.only_user_id,
                          fetch_next_episode=not args.no_rss)
            print(json.dumps(res, indent=2, default=str))
            return 0
        if args.campaign not in L.CAMPAIGNS:
            print(f'campaign must be one of {L.CAMPAIGNS}', file=sys.stderr)
            return 2
        user = A.db.session.get(A.User, args.user_id)
        if user is None:
            print('no such user', file=sys.stderr)
            return 2
        if args.cmd == 'render':
            ctx = L.build_context(args.campaign, user,
                                  public_base=A.PUBLIC_BASE_URL,
                                  secret_key=A.app.secret_key)
            subject, text, html = L.build_bodies(args.campaign, ctx)
            os.makedirs(args.out, exist_ok=True)
            stem = os.path.join(args.out, args.campaign)
            with open(stem + '.html', 'w', encoding='utf-8') as fh:
                fh.write(html)
            with open(stem + '.txt', 'w', encoding='utf-8') as fh:
                fh.write(f'Subject: {subject}\n\n{text}')
            print(json.dumps({'subject': subject, 'variant': ctx.get('variant')}))
            return 0
        if args.cmd == 'test-send':
            out = L.send_one(args.campaign, user, public_base=A.PUBLIC_BASE_URL,
                             secret_key=A.app.secret_key, to_override=args.to,
                             claim=False)
            print(out)
            return 0 if out == 'sent' else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
