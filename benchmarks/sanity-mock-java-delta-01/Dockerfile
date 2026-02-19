# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
################################################################################

FROM ghcr.io/aixcc-finals/base-builder-jvm:v1.3.0

RUN apt-get update && apt-get install -y maven

#RUN git clone git@github.com:Team-Atlanta/mock-java.git $SRC/src/mock_java

COPY pkgs/mock_java.tar.gz $SRC/src/mock_java.tar.gz
RUN tar -xzf $SRC/src/mock_java.tar.gz -C $SRC/src/ && rm $SRC/src/mock_java.tar.gz

WORKDIR $SRC/src/mock_java

COPY fuzz $SRC/src/fuzz
COPY build.sh $SRC/
