#!/usr/bin/env python3

import sys

def main():
    path = ""
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        print("Give info path\n")
        exit()

    dataMap = {}
    addr = []
    with open(path, "r") as fp:
        data = fp.readlines()

    for entry in data:
        tmp = entry.strip('\n')
        tmp = tmp.split(' ')
        baddr = int(tmp[0])
        bsize = int(tmp[1])
        addr.append(baddr)
        dataMap[baddr] = bsize

    print(f"Number of blocks: {len(addr)}")

    addr.sort()

    for blk in addr:
        print(f'{blk} {dataMap[blk]}')

if __name__ == "__main__":
    main()
