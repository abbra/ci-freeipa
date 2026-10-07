# freeipa-ci base image (A1).
#
# A systemd-capable Fedora toolbox image: the unit of test execution is a
# container running systemd as PID 1, booted to multi-user.target with sshd.
# See doc/designs/ci_modernization.md, layer 1.
#
# Design decisions encoded here:
#   * firewalld is INSTALLED but MASKED. An active firewalld inside a
#     container REJECTs inter-container traffic (zone hooks filter even an
#     unbound interface). Network isolation for CI is the podman network.
#     Tests that need firewall rules can unmask it in their setup.
#   * systemd-resolved is masked: /etc/resolv.conf is owned by the env
#     provisioner, not by the container.
#   * sshd with root key login: the multihost transport (pytest framework)
#     connects to every host as root.

# The dist follows the caller (images/build.sh --dist N passes it here; the
# dist-git lanes pass the guest's dist so RPMs built for that dist resolve).
# `rawhide` is a valid tag (the rolling toolbox image).
ARG DIST=44
FROM registry.fedoraproject.org/fedora-toolbox:${DIST}

# Container contract: systemd init, sshd, firewall tooling (installed,
# masked), DNS/NS utilities, build tools the test suites expect on a
# developer host.
RUN dnf -y install --setopt=install_weak_deps=False \
        systemd systemd-container \
        openssh-server \
        firewalld \
        nss-tools sudo git bind-utils hostname chrony \
    && dnf clean all

# chronyd stays active: ipa-server-install (without --no-ntp, as the Azure
# base jobs run) restarts it; in a container it just tracks the host clock.
RUN systemctl mask firewalld systemd-resolved

RUN printf 'PermitRootLogin yes\nPasswordAuthentication no\n' \
        > /etc/ssh/sshd_config.d/99-freeipa-ci.conf \
    && systemctl enable sshd

RUN systemctl set-default multi-user.target

# systemd in a container: stop via SIGRTMIN+3 (clean shutdown path).
STOPSIGNAL RTMIN+3
ENTRYPOINT ["/usr/sbin/init"]
