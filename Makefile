.PHONY: build build-local setup	use clean-local clean

build: use
	docker buildx build --platform linux/amd64,linux/arm64 -t public.ecr.aws/x3l4a9v5/ecsnlbsidecar\:latest .

build-and-push: use
	docker buildx build --platform linux/amd64,linux/arm64 -t public.ecr.aws/x3l4a9v5/ecsnlbsidecar:latest --push .

build-local:
	docker build -t public.ecr.aws/x3l4a9v5/ecsnlbsidecar:local .

setup:
	docker buildx create --name multi-arch

use:
	docker buildx use multi-arch

clean-local:
	docker rmi public.ecr.aws/x3l4a9v5/ecsnlbsidecar:local
