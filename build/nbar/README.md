# NBAR decoder for Linux AMD64

The collector image copies this executable from the Git checkout. Regenerate it after changing the NBAR producer, its GoFlow2 version or its Go toolchain. Run this command from the repository root on a build host with access to the Go toolchain image and module proxy.

```sh
docker build --platform linux/amd64 -f collector/nbar/Dockerfile \
  --output type=local,dest=build/nbar .
```

Commit the regenerated executable with the source changes. The integration workflow rebuilds the decoder and checks it against the committed executable.
