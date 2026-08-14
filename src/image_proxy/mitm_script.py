"""mitmdump script entrypoint for the image proxy addon."""

from __future__ import annotations

from image_proxy.addon import ImageProxyAddon


addons = [ImageProxyAddon()]
