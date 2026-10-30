#!/bin/sh
# Steady traffic for the lab's interface graphs: iperf3 to one address, at a rate
# that changes every minute. containerlab starts it in the background on the senders
# at deploy (host1 and host3 in netgpt.clab.yml, the users in wan/wan.clab.yml), and
# it runs until the lab is destroyed. A run that fails (the far end not up yet, or
# one of the gremlin's faults in the way) is tried again after a pause.
#
# The port is the server's iperf3 listener, 5201 unless given; a listener serves one
# client at a time. A rate is 1 to 9 steps, in Mbit/s, and a step is 5 unless given,
# so 5 to 45 Mbit/s.
#
#   sh /traffic.sh 192.168.12.2
#   sh /traffic.sh 10.10.100.10 5202 1

server="$1"
port="${2:-5201}"
step="${3:-5}"
while true; do
    # From /dev/urandom: awk's srand() seeds from the clock, and two hosts started in
    # the same second would pick the same rate every minute.
    rate=$(( step * (1 + $(od -An -N1 -tu1 /dev/urandom) % 9) ))
    iperf3 -c "$server" -p "$port" -t 60 -b "${rate}M" > /dev/null 2>&1 || sleep 5
done
