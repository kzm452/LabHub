"""LabHub v5 reservation reminder worker.

Use: python run_reminders.py --loop   (in another PowerShell with LINE secrets set)
Use: python run_reminders.py          (one-off check; suitable for Task Scheduler)
"""
import argparse
import time
from server import connect_db, now_ms, push_line_text
from labhub_features import send_due_reservation_reminders


def check_once():
    count=send_due_reservation_reminders(connect_db, now_ms, push_line_text)
    print(f'LINE設備予約リマインド: {count}件を送信', flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--loop', action='store_true', help='1分ごとに実行（テスト用）')
    args=parser.parse_args()
    if args.loop:
        print('LabHub v5 リマインダー待機中 (Ctrl+C で停止)',flush=True)
        while True:
            check_once()
            time.sleep(60)
    else:
        check_once()
