#!/bin/sh
# Steady traffic for the lab's interface graphs: iperf3 to one address, at a rate
# that changes every minute, between 5 and 45 Mbit/s. containerlab starts it in the
# background on host1 and host3 at deploy (netgpt.clab.yml), and it runs until the
# lab is destroyed. A run that fails (the far end not up yet, or one of the
# gremlin's faults in the way) is tried again after a pause.
#
#   sh /traffic.sh 192.168.12.2

server="$1"
while true; do
    # From /dev/urandom: awk's srand() seeds from the clock, and two hosts started in
    # the same second would pick the same rate every minute.
    rate=$(( 5 * (1 + $(od -An -N1 -tu1 /dev/urandom) % 9) ))
    iperf3 -c "$server" -t 60 -b "${rate}M" > /dev/null 2>&1 || sleep 5
done
