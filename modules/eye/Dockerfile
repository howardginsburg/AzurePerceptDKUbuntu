# Azure Percept Eye module - slim, locally-built image.
#
# Stage 1 compiles the inference app from ./app (upstream:
# microsoft/azure-percept-advanced-development/azureeyemodule/app) inside the
# original Microsoft image, which carries the toolchain plus the Myriad X
# stack (OpenCV G-API/MX, mxIf, onnxruntime, OpenVINO, GStreamer).
# Stage 2 is FROM scratch and holds only the runtime closure.
ARG BASE=mcr.microsoft.com/azureedgedevices/azureeyemodule:2301-1-noauth

FROM ${BASE} AS build
USER root
ARG BUILD_TYPE=Release
ARG JOBS=2
# Libraries for the inference outputs (app/output): Eclipse Paho MQTT C
# (EPL-2.0 / EDL-1.0) built static with OpenSSL, and the header-only
# cpp-httplib (MIT). Pinned versions, verified by SHA-256; their licence
# texts are copied into the image under /usr/share/licenses.
ARG PAHO_VERSION=1.3.16
ARG PAHO_SHA256=8b960f51edc7e03507637d987882bc486d8f4be6e79431bf99e2763344fd14c5
ARG HTTPLIB_VERSION=0.65.0
ARG HTTPLIB_SHA256=17d85dcf129d3cc67eefba846608016f43882c9de7fae811a930f9736a10eeaa
ARG HTTPLIB_LICENSE_SHA256=4b45cbe16d7b71b89ae6127e26e0d90a029198ca5e958ad8e3d0b8bbed364d8b
RUN set -e; T=/third_party; mkdir -p $T/src $T/include $T/licenses/paho.mqtt.c $T/licenses/cpp-httplib; cd $T/src; \
    curl -fsSL -o paho.tar.gz https://github.com/eclipse-paho/paho.mqtt.c/archive/refs/tags/v${PAHO_VERSION}.tar.gz; \
    echo "${PAHO_SHA256}  paho.tar.gz" | sha256sum -c -; \
    tar xzf paho.tar.gz; P=paho.mqtt.c-${PAHO_VERSION}; \
    cmake -S $P -B paho-build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=$T -DCMAKE_INSTALL_LIBDIR=lib \
          -DPAHO_WITH_SSL=TRUE -DPAHO_BUILD_SHARED=FALSE -DPAHO_BUILD_STATIC=TRUE \
          -DPAHO_ENABLE_TESTING=FALSE -DPAHO_ENABLE_CPACK=FALSE -DPAHO_HIGH_PERFORMANCE=TRUE; \
    cmake --build paho-build -j${JOBS} --target install; \
    cp $P/LICENSE $P/NOTICE $P/edl-v10 $P/epl-v20 $P/about.html $P/notice.html $T/licenses/paho.mqtt.c/; \
    curl -fsSL -o $T/include/httplib.h https://raw.githubusercontent.com/yhirose/cpp-httplib/v${HTTPLIB_VERSION}/httplib.h; \
    curl -fsSL -o $T/licenses/cpp-httplib/LICENSE https://raw.githubusercontent.com/yhirose/cpp-httplib/v${HTTPLIB_VERSION}/LICENSE; \
    printf '%s  %s\n' "${HTTPLIB_SHA256}" $T/include/httplib.h "${HTTPLIB_LICENSE_SHA256}" $T/licenses/cpp-httplib/LICENSE | sha256sum -c -; \
    rm -rf $T/src
COPY app /src/app
RUN mkdir -p /src/build && cd /src/build && \
    cmake -DOpenCV_DIR=/eyesom/build/install/lib64/cmake/opencv4 \
          -DmxIf_DIR=/eyesom/mxif/install/share/mxIf \
          -DCMAKE_PREFIX_PATH=/onnxruntime/install/lib64 \
          -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
          -DAUTH_FIX=1 ../app && \
    make -j${JOBS}
COPY collect-rootfs.sh /collect-rootfs.sh
RUN bash /collect-rootfs.sh /rootfs
# Turns the EYE_* model and video settings into the app's options.
COPY entrypoint.sh /rootfs/app/entrypoint.sh
RUN chmod 755 /rootfs/app/entrypoint.sh

FROM scratch
COPY --from=build /rootfs/ /
ENV PATH=/usr/bin:/usr/sbin HOME=/app
WORKDIR /app
USER 5001:5000
EXPOSE 8554 8555
ENTRYPOINT ["/app/entrypoint.sh"]
