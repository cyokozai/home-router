# pc03（受信側の端末・BR 役）のイメージ。netshoot に、NGN 役（lab/pc03-ngn.sh）で使う
# DHCPv6 サーバ（Kea。IA_PD でプレフィックスを委任する）と radvd（RA を出す）を足す。
# 土台は pc01 と同じ netshoot の版（tests/test_lab_topology.py が確かめる）。
#
#   docker build -t home-router/pc03-ngn:lab -f lab/pc03-ngn.Dockerfile lab
FROM nicolaka/netshoot:v0.14

RUN apk add --no-cache kea-dhcp6 radvd \
 && mkdir -p /var/lib/kea /run/kea /run/radvd \
 && kea-dhcp6 -v \
 && command -v radvd
