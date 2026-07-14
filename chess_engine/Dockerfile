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

# Build LC0. The build script will detect onnxruntime in /usr/local
RUN ./build.sh

# Stage 2: Create the standalone service image
FROM debian:bookworm-slim

# Install runtime dependencies:
# - socat to bridge UCI to TCP
# - curl to download weights
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the compiled binary from the builder stage
COPY --from=builder /build/lc0/build/release/lc0 .

# Copy the ONNX Runtime shared libraries from the builder stage
COPY --from=builder /usr/local/lib/libonnxruntime.so* /usr/local/lib/
RUN ldconfig

# Download LC0 ONNX weights (using a standard stable network)
RUN curl -L "https://github.com/LeelaChessZero/lc0-networks/raw/master/onnx/781000.onnx" -o weights.onnx

# Expose the UCI port
EXPOSE 3333

# Use socat to bridge the TCP port 3333 to the LC0 engine's stdin/stdout
CMD ["socat", "TCP-LISTEN:3333,fork,reuseaddr", "EXEC:'./lc0 --weights=weights.onnx',stderr"]
