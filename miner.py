import argon2
import argparse
import base64
import hashlib
import math
import multiprocessing
import os
import random
import re
import requests
import time

POOL_URL = ''
WALLET_ADDRESS = '4boSzKSto9SqkZFxExYXhC4UnPrqqzvQ78QjABSSqXTA2JixFU1g9tBmkGZPzKtQNeDkhkvS7vmED1KuSFY33Egc'
WORKER_NAME = hashlib.sha224((os.uname()[1]).encode("utf-8")).hexdigest()[0:32]
WORKER_COUNT = math.ceil((multiprocessing.cpu_count() + 1) / 2)


def update_work(work_item, work_item_lock, hash_rates):
    update_count = 0
    while True:
        try:
            r = requests.get(
                '%s/mine.php?q=info&worker=%s&address=%s&hashrate=%s' %
                (POOL_URL, WORKER_NAME, WALLET_ADDRESS, sum(hash_rates)),
                timeout=1)
            r.raise_for_status()
            data = r.json()['data']
            if data is None:
                raise ValueError('data=None')
            block = data['block']
            if block is None:
                raise ValueError('block=None')
            height = data['height']
            if height is None:
                raise ValueError('height=None')
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
                work_item[0] = block
                work_item[1] = difficulty
                work_item[2] = limit
                work_item[3] = pool_address
                work_item[4] = height
            if update_count % 10 == 0:
                print("update_work:\n", r.json())
            update_count += 1
            time.sleep(5)
        except Exception as e:
            print("update_work failed, retry in 30s:\n", e)
            time.sleep(30)


def submit_share(nonce, argon, pool_address):
    argon = argon[30:]
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
                },
                timeout=1)
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


def solve_work(index, work_item, work_item_lock, result_queue, hash_rates):
    work_count = 0
    time_start = time.time()
    while (True):
        with work_item_lock:
            (block, difficulty, limit, pool_address, height) = work_item

        nonce = base64.b64encode(
            random.getrandbits(256).to_bytes(32,
                                             byteorder='big')).decode('utf-8')
        nonce = re.sub('[^a-zA-Z0-9]', '', nonce)
        base = '%s-%s-%s-%s' % (pool_address, nonce, block, difficulty)
        if height > 10800:
            ph = argon2.PasswordHasher(
                time_cost=1, memory_cost=524288, parallelism=1)
        else:
            ph = argon2.PasswordHasher(
                time_cost=4, memory_cost=16384, parallelism=4)
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
            print("solve_work: #%d found valid nonce: %s, %s, %s @ %s:%s:%s" %
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
                print('%f H/s - %d workers' % (sum(hash_rates),
                                               len(hash_rates)))


def main():
    global POOL_URL
    global WALLET_ADDRESS
    global WORKER_NAME
    global WORKER_COUNT

    parser = argparse.ArgumentParser(description='Arionum pool miner')
    parser.add_argument(
        '--pool',
        type=str,
        default='http://aropool.com',
        help='Mining pool URL')
    parser.add_argument(
        '--wallet', type=str, default=None, help='Arionum wallet for deposits')
    parser.add_argument(
        '--worker_name', type=str, default=None, help='Worker name')
    parser.add_argument(
        '--worker_count',
        type=int,
        default=None,
        help='Number of workers to use')
    args = parser.parse_args()

    POOL_URL = args.pool
    if args.wallet is not None:
        WALLET_ADDRESS = args.wallet
    if args.worker_name is not None:
        WORKER_NAME = args.worker_name
    if args.worker_count is not None:
        WORKER_COUNT = args.worker_count
    print("Launching miner with worker name: ", WORKER_NAME)
    print("Mining to wallet: ", WALLET_ADDRESS)

    with multiprocessing.Manager() as manager:
        hash_rates = manager.Array('f', range(WORKER_COUNT))
        work_item = manager.list([None for _ in range(5)])
        work_item_lock = manager.Lock()
        result_queue = manager.Queue()

        p = multiprocessing.Process(
            target=update_work, args=(work_item, work_item_lock, hash_rates))
        p.start()

        while work_item[0] is None:
            time.sleep(1)

        processes = []
        for i in range(WORKER_COUNT):
            p = multiprocessing.Process(
                target=solve_work,
                args=(i, work_item, work_item_lock, result_queue, hash_rates))
            processes.append(p)
            p.start()
            print("started worker: %d" % (i))

        while True:
            (nonce, argon, pool_address) = result_queue.get()
            submit_share(nonce, argon, pool_address)
            result_queue.task_done()


if __name__ == '__main__':
    main()
