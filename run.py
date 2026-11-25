#!/usr/bin/env python3
"""Reproduce the final submission.

    python3 run.py                       # uses ./data, writes submission/submission_reproduced.csv
    python3 run.py --out my.csv

Also prints accuracy on the 40 worked examples and verifies the result is
byte-identical to the committed submission/submission.csv.
"""
import argparse
import csv
import filecmp
import os
import sys

from procurement_desk import engine

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--playbook', default=os.path.join(HERE, 'data', 'playbook'))
    ap.add_argument('--threads', default=os.path.join(HERE, 'data', 'threads.csv'))
    ap.add_argument('--samples', default=os.path.join(HERE, 'data', 'sample_threads.csv'))
    ap.add_argument('--answers', default=os.path.join(HERE, 'data', 'sample_answers.csv'))
    ap.add_argument('--out', default=os.path.join(HERE, 'submission', 'submission_reproduced.csv'))
    a = ap.parse_args()

    s_out, _ = engine.run(a.samples, a.playbook)
    with open(a.answers, encoding='utf-8') as fh:
        ans = {r['thread_id']: r['label'] for r in csv.DictReader(fh)}
    ok = sum(s_out[t] == lab for t, lab in ans.items())
    print('worked examples matched: %d/%d' % (ok, len(ans)))

    out, _ = engine.run(a.threads, a.playbook)
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(['thread_id', 'label'])
        for t, lab in out.items():
            w.writerow([t, lab])
    print('wrote %d rows to %s' % (len(out), a.out))

    final = os.path.join(HERE, 'submission', 'submission.csv')
    if os.path.exists(final) and os.path.abspath(a.out) != final:
        same = filecmp.cmp(a.out, final, shallow=False)
        print('identical to submission/submission.csv:', same)
        sys.exit(0 if same else 1)


if __name__ == '__main__':
    main()
