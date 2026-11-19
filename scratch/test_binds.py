from tutti import netpolicy
ifaces = netpolicy.interfaces()
print(netpolicy.binds('auto', 'dashboard.bind', ifaces))
