#!/bin/bash
# Stock Mariner drops new SSH connections from any IP that opens 6 within 60 s
# (iptables "recent" list DEFAULT -> chain SSHBRUTEFORCE). percept-kit makes many short
# connections, so exempt this PC's IP. Runtime only: the rule is gone after a reboot.
ip=${SSH_CLIENT%% *}
if [ -n "$ip" ] && command -v iptables >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  for t in raw mangle nat; do
    if sudo -n iptables -t "$t" -S PREROUTING 2>/dev/null | grep -q SSHBRUTEFORCE; then
      if ! sudo -n iptables -t "$t" -C PREROUTING -s "$ip/32" -p tcp --dport 22 -j ACCEPT 2>/dev/null; then
        sudo -n iptables -t "$t" -I PREROUTING 1 -s "$ip/32" -p tcp --dport 22 -j ACCEPT
      fi
      echo "sshexempt=$ip"
    fi
  done
fi
