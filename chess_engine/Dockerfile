# Stage 1: Build LC0 from source
FROM debian:bookworm-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    git \
    meson \
    ninja-build \
    g++ \
    pkg-config \
    wget \
    python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Manually download and install ONNX Runtime since it's not in Bookworm's default repos
ARG ONNX_VERSION=1.17.1
RUN wget https://github.com/microsoft/onnxruntime/releases/download/v${ONNX_VERSION}/onnxruntime-linux-x64-${ONNX_VERSION}.tgz \
    && tar -xzf onnxruntime-linux-x64-${ONNX_VERSION}.tgz \
    && cp -r onnxruntime-linux-x64-${ONNX_VERSION}/include/* /usr/local/include/ \
    && cp -r onnxruntime-linux-x64-${ONNX_VERSION}/lib/* /usr/local/lib/ \
    && ldconfig

# Clone LC0 repository with submodules
RUN git clone --recursive https://github.com/LeelaChessZero/lc0.git

WORKDIR /build/lc0

# Build LC0 with the ONNX backend enabled. onnxruntime was installed under
# /usr/local, but meson defaults to /usr/lib and /usr/include/onnxruntime, so
# without these flags it would not find it and would silently drop the onnx
# backend (leaving only the slower eigen fallback). build.sh forwards extra
# args to `meson setup`.
RUN ./build.sh -Donnx_libdir=/usr/local/lib -Donnx_include=/usr/local/include

# Stage 2: Create the standalone service image
FROM debian:bookworm-slim

# Install runtime dependencies:
# - socat to bridge UCI to TCP
# - curl to download weights
# - zlib1g-dev provides libz.so, which the compiled lc0 binary links against
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    socat \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the compiled binary from the builder stage
COPY --from=builder /build/lc0/build/release/lc0 .

# Copy the ONNX Runtime shared libraries from the builder stage
COPY --from=builder /usr/local/lib/libonnxruntime.so* /usr/local/lib/
RUN ldconfig

# Download a standard stable LC0 network (128x10, small & fast on CPU).
# --fail makes a bad URL abort the build instead of silently saving an HTML
# error page, which lc0 would then reject as "unparseable" weights.
RUN curl -fL "https://storage.lczero.org/files/networks-contrib/744706.pb.gz" -o weights.pb.gz

# Expose the UCI port
EXPOSE 3333

# Use socat to bridge the TCP port 3333 to the LC0 engine's stdin/stdout.
# --backend=onnx-cpu runs inference through onnxruntime's optimized CPU kernels
# (faster than the eigen fallback); the .pb.gz net is converted to ONNX in
# memory at load time, so no pre-converted .onnx file is needed.
CMD ["socat", "TCP-LISTEN:3333,fork,reuseaddr", "EXEC:'./lc0 --backend=onnx-cpu --weights=weights.pb.gz',stderr"]
