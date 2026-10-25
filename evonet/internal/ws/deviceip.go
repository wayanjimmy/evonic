package ws

import (
	"net"
	"sort"
	"strings"
)

// maxDeviceIPs caps how many addresses are reported in X-Device-IPs.
const maxDeviceIPs = 4

// virtualIfacePrefixes name container/VM bridges whose addresses only exist
// inside this machine, so reporting them would just be noise.
var virtualIfacePrefixes = []string{
	"docker", "br-", "veth", "virbr", "vmnet", "vboxnet",
	"cni", "flannel", "cali", "lxc", "lxd", "podman", "kube",
}

// deviceIPs lists the addresses of this machine's up, non-loopback, non-virtual
// interfaces, best first (see rankIPs).
func deviceIPs() []string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil
	}
	var ips []net.IP
	for _, iface := range ifaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 || isVirtualInterface(iface.Name) {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, a := range addrs {
			if ipnet, ok := a.(*net.IPNet); ok {
				ips = append(ips, ipnet.IP)
			}
		}
	}
	return rankIPs(ips)
}

func isVirtualInterface(name string) bool {
	for _, p := range virtualIfacePrefixes {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	return false
}

// rankIPs keeps unique global unicast addresses (dropping loopback, link-local
// and multicast), orders IPv4 before IPv6 and public before private, and caps
// the result at maxDeviceIPs.
func rankIPs(ips []net.IP) []string {
	seen := make(map[string]bool)
	var kept []net.IP
	for _, ip := range ips {
		if ip == nil || !ip.IsGlobalUnicast() {
			continue
		}
		if v4 := ip.To4(); v4 != nil {
			ip = v4
		}
		if s := ip.String(); !seen[s] {
			seen[s] = true
			kept = append(kept, ip)
		}
	}
	rank := func(ip net.IP) int {
		r := 0
		if ip.To4() == nil {
			r += 2
		}
		if ip.IsPrivate() {
			r++
		}
		return r
	}
	sort.SliceStable(kept, func(i, j int) bool { return rank(kept[i]) < rank(kept[j]) })
	if len(kept) > maxDeviceIPs {
		kept = kept[:maxDeviceIPs]
	}
	out := make([]string, len(kept))
	for i, ip := range kept {
		out[i] = ip.String()
	}
	return out
}
