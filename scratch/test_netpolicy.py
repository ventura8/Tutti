import ipaddress
PRIVATE = [ipaddress.ip_network(n) for n in ('127.0.0.0/8', '::1/128', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                                               '100.64.0.0/10', 'fc00::/7', 'fe80::/10')]

def private(addr):
    ip = ipaddress.ip_address(addr.split('%', 1)[0])
    return any(ip in n for n in PRIVATE if n.version == ip.version)

print(f"127.0.0.1 is private: {private('127.0.0.1')}")
print(f"192.168.1.33 is private: {private('192.168.1.33')}")
print(f"8.8.8.8 is private: {private('8.8.8.8')}")
