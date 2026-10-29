"""
The inventory module defines which devices NetGPT may reach and how it logs in.

The model only ever names devices from the inventory; it cannot point NetGPT at
an arbitrary host. Secrets never live in the inventory file itself. Each
credentials profile names an environment variable or a file (readable only by
its owner) that holds the password, or an SSH private key file (likewise).
"""

from __future__ import annotations

import ipaddress
import os
import stat
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator


class InventoryError(ValueError):
    """Raised when the inventory is invalid or a device or credential cannot be resolved."""


def owner_only(path: Path, what: str) -> Path:
    """The path, once it is known to exist and to be readable by its owner alone."""
    path = path.expanduser()
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise InventoryError(f"{what} {path} does not exist.") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise InventoryError(f"{what} {path} is readable by others; chmod 600 it.")
    return path


class Credentials(BaseModel):
    """A login profile. Exactly one of password_env, password_file, or ssh_key_file must be set."""

    username: str
    password_env: str | None = None
    password_file: Path | None = None
    ssh_key_file: Path | None = None

    @model_validator(mode="after")
    def _one_secret(self) -> Credentials:
        if [self.password_env, self.password_file, self.ssh_key_file].count(None) != 2:
            raise ValueError("Set exactly one of password_env, password_file, or ssh_key_file.")
        return self

    def password(self) -> str:
        """Resolve the password from its environment variable or file."""
        if self.password_env is not None:
            value = os.environ.get(self.password_env)
            if value is None:
                raise InventoryError(f"Environment variable {self.password_env} is not set.")
            return value
        if self.password_file is None:
            raise InventoryError(f"The {self.username} login uses an SSH key, and this platform needs a password.")
        return owner_only(self.password_file, "Password file").read_text().strip()

    def ssh_key(self) -> Path:
        """The SSH private key file, checked but not read: the SSH client reads it."""
        return owner_only(self.ssh_key_file, "SSH key file")


class Device(BaseModel):
    """A device NetGPT may query."""

    name: str
    host: str
    platform: str
    credentials: str
    role: str | None = None
    tls_ca: Path | None = None
    # The SSH host keys to trust, for platforms reached over SSH. None means the user's ~/.ssh/known_hosts.
    known_hosts: Path | None = None


class Inventory(BaseModel):
    """
    The set of devices and credentials profiles NetGPT works with, plus the
    networks the NetGPT host itself may ping sweep.
    """

    credentials: dict[str, Credentials]
    devices: list[Device]
    sweep_networks: list[ipaddress.IPv4Network] = []

    @model_validator(mode="after")
    def _check_references(self) -> Inventory:
        names = [device.name for device in self.devices]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate device names: {', '.join(duplicates)}")
        for device in self.devices:
            if device.credentials not in self.credentials:
                raise ValueError(f"Device {device.name} uses unknown credentials profile {device.credentials}.")
        return self

    @classmethod
    def load(cls, path: Path) -> Inventory:
        """Load an inventory from YAML. Relative file paths resolve against the file's directory."""
        path = path.expanduser().resolve()
        with path.open() as handle:
            inventory = cls.model_validate(yaml.safe_load(handle))
        def resolve(file: Path | None) -> Path | None:
            return path.parent / file if file is not None and not file.expanduser().is_absolute() else file

        for device in inventory.devices:
            device.tls_ca, device.known_hosts = resolve(device.tls_ca), resolve(device.known_hosts)
        for profile in inventory.credentials.values():
            profile.password_file, profile.ssh_key_file = resolve(profile.password_file), resolve(profile.ssh_key_file)
        return inventory

    def get(self, name: str) -> Device:
        """Return the named device, or raise InventoryError listing the known names."""
        for device in self.devices:
            if device.name == name:
                return device
        known = ", ".join(device.name for device in self.devices)
        raise InventoryError(f"Unknown device '{name}'. Known devices: {known}.")

    def check_sweep(self, network: ipaddress.IPv4Network) -> None:
        """Raise InventoryError unless the network lies inside one of the sweep networks."""
        if not any(network.subnet_of(allowed) for allowed in self.sweep_networks):
            allowed = ", ".join(str(net) for net in self.sweep_networks) or "none configured"
            raise InventoryError(f"{network} is outside the inventory's sweep networks ({allowed}).")

    def credentials_for(self, device: Device) -> Credentials:
        """Return the credentials profile for a device."""
        return self.credentials[device.credentials]
