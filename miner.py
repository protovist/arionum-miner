import argparse
import base64
import hashlib
import os
import queue
import random
import re
import requests
import threading
import time

from argon2 import PasswordHasher

POOL_URL = ''
WALLET_ADDRESS = '4boSzKSto9SqkZFxExYXhC4UnPrqqzvQ78QjABSSqXTA2JixFU1g9tBmkGZPzKtQNeDkhkvS7vmED1KuSFY33Egc'
WORKER_NAME = hashlib.sha224((os.uname()[1]).encode("utf-8")).hexdigest()[0:32]

hash_rates = []
work_item = []
result_queue = queue.Queue()
work_item_lock = threading.Lock()


def update_work():
    global work_item
    update_count = 0
    while True:
        try:
            r = requests.get(
                '%s/mine.php?q=info&worker=%s&address=%s&hashrate=%s' %
                (POOL_URL, WORKER_NAME, WALLET_ADDRESS, sum(hash_rates)))
            r.raise_for_status()
            data = r.json()['data']
            if data is None:
                raise ValueError('data=None')
            block = data['block']
            if block is None:
                raise ValueError('block=None')
            difficulty = data['difficulty']
            if difficulty is None:
                raise ValueError('difficulty=None')
            limit = data['limit']
            if limit is None:
                raise ValueError('limit=None')
            pool_address = data['public_key']
            if pool_address is None:
                raise ValueError('public_key=None')

            with work_item_lock:
                work_item = (block, difficulty, limit, pool_address)
            if update_count % 10 == 0:
                print("update_work:\n", r.json())
            update_count += 1
            time.sleep(5)
        except Exception as e:
            print("update_work failed, retry in 30s:\n", e)
            time.sleep(30)


def submit_share(nonce, argon, pool_address):
    argon = argon[29:]
    print("submit_share: %s, %s" % (nonce, argon))
    share_submitted = False
    try:
        retry_count = 0
        while not share_submitted and retry_count < 5:
            r = requests.post(
                '%s/mine.php?q=submitNonce' % POOL_URL,
                data={
                    'argon': argon,
                    'nonce': nonce,
                    'private_key': WALLET_ADDRESS,
                    'public_key': pool_address,
                    'address': WALLET_ADDRESS,
                })
            r.raise_for_status()
            share_submitted = True
            print("submit_share:\n", r.json())
    except Exception as e:
        print("submit_share failed, retry in 5s:\n", e)
        retry_count += 1
        time.sleep(5)
    finally:
        if retry_count == 5:
            print("submit_share failed after 5 attempts\n")


def solve_work(index):
    global hash_rates
    work_count = 0
    time_start = time.time()
    while (True):
        with work_item_lock:
            (block, difficulty, limit, pool_address) = work_item

        nonce = base64.b64encode(
            random.getrandbits(256).to_bytes(32,
                                             byteorder='big')).decode('utf-8')
        nonce = re.sub('[^a-zA-Z0-9]', '', nonce)
        base = '%s-%s-%s-%s' % (pool_address, nonce, block, difficulty)
        ph = PasswordHasher(time_cost=4, memory_cost=16384, parallelism=4)
        argon = ph.hash(base)
        base = base + argon
        hash = hashlib.sha512(base.encode('utf-8'))
        for i in range(4):
            hash = hashlib.sha512(hash.digest())
        digest = hashlib.sha512(hash.digest()).hexdigest()
        m = [digest[i:i + 2] for i in range(0, len(digest), 2)]
        duration = '%d%d%d%d%d%d%d%d' % (int(m[10], 16), int(m[15], 16),
                                         int(m[20], 16), int(m[23], 16),
                                         int(m[31], 16), int(m[40], 16),
                                         int(m[45], 16), int(m[55], 16))
        result = int(duration) // int(difficulty)

        if result > 0 and result < limit:
            print("solve_work: t%d found valid nonce: %s, %s, %s @ %s:%s:%s" %
                  (index, nonce, argon, pool_address, duration, difficulty,
                   result))
            result_queue.put((nonce, argon, pool_address))

        work_count += 1
        time_end = time.time()
        hash_rates[index] = work_count / (time_end - time_start)
        if work_count == 100:
            work_count = 0
            time_start = time_end
            if index == 0:
                print('%f H/s - %d threads' % (sum(hash_rates),
                                               len(hash_rates)))


def main():
    global POOL_URL
    global WALLET_ADDRESS
    global WORKER_NAME

    parser = argparse.ArgumentParser(description='Arionum pool miner')
    parser.add_argument(
        '--pool',
        type=str,
        default='http://aropool.com',
        help='Mining pool URL')
    parser.add_argument(
        '--wallet', type=str, default='', help='Arionum wallet for deposits')
    parser.add_argument('--worker', type=str, default='', help='Worker name')
    parser.add_argument(
        '--threads', type=int, default=2, help='Number of threads to use')
    args = parser.parse_args()

    POOL_URL = args.pool
    if args.wallet:
        WALLET_ADDRESS = args.wallet
    if args.worker:
        WORKER_NAME = args.worker
    print("Launching miner with worker name: ", WORKER_NAME)
    print("Mining to wallet: ", WALLET_ADDRESS)

    t = threading.Thread(target=update_work)
    t.daemon = True
    t.start()

    print("Waiting for work from pool...")
    while len(work_item) == 0:
        time.sleep(1)

    threads = []
    for i in range(args.threads):
        t = threading.Thread(target=solve_work, args=(i, ))
        threads.append(t)
        hash_rates.append(0)
        t.daemon = True
        t.start()
        print("started thread: %d" % (i))

    while True:
        (nonce, argon, pool_address) = result_queue.get()
        submit_share(nonce, argon, pool_address)
        result_queue.task_done()

    for t in threads:
        t.join()


if __name__ == '__main__':
    main()
