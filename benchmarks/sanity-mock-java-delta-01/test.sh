#!/bin/bash

cd "$OUT/src/mock_java"
mvn test -DskipTests=false
