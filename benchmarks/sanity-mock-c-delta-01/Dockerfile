FROM ghcr.io/aixcc-finals/base-builder:v1.3.0

#RUN git clone git@github.com:Team-Atlanta/mock-c.git

COPY pkgs/mock-c.tar.gz $SRC/mock-c.tar.gz
RUN tar -xzf $SRC/mock-c.tar.gz -C $SRC/ && rm $SRC/mock-c.tar.gz

WORKDIR $SRC/mock-c

COPY fuzz $SRC/fuzz
COPY build.sh $SRC/
COPY tests $SRC/tests
