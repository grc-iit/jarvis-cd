# IO500

## IOR 

blocksize: Each rank read/writes data in contiguous blocks.
This is the size of that block.
By default 1MB.

transfer size: blocks are divided into multiple consecutive I/O calls.
E.g., if transfer size is 4KB, a 1MB block will be dived into 2^20/4*2^10 = 256 I/O calls.

segmentCount: the number of blocks to write per-rank. A better name would've been blockCount.

## MDTEST

