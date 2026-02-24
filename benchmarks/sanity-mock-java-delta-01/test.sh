#!/bin/bash

cd "$OUT/mock_java"
mvn test -DskipTests=false
