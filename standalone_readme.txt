readme.txt for standalone executable files that can be downloaded from
https://github.com/jeremiah-k/mtjk/releases

If you do not want to install Python and/or the Python libraries, download the
`mtjk_ubuntu` executable. Then run:

```sh
chmod +x mtjk_ubuntu
./mtjk_ubuntu --help
```

`meshtastic_ubuntu` is published from the same build as a compatibility name for
existing workflows.

See https://meshtastic.org/docs/software/python/cli/installation/#standalone-installation-ubuntu-only
for upstream standalone-installation background.

This standalone build includes the core mtjk CLI and the optional `cli` extras.
It does not bundle the separate tunnel, analysis, or power-monitor dependency
stacks; install mtjk with Python/uv when those optional features are needed.

The Python package namespace remains `meshtastic`; the standalone executable name
does not change the public Python API.
