# ci overlay

A skeleton, not a working overlay. It exists so the shape of the CI deploy is
settled: same base, image from GHCR, a `ghcr` image pull secret on the two pod
specs that run it.

`kubectl kustomize kubernetes/overlays/ci` fails today with "no such file or
directory: secrets/postgres.env". That is expected. Phase 5 writes those env
files in the workflow (from repository secrets, never committed) and creates
the `ghcr` docker-registry Secret in `openmodel-api` before applying.
