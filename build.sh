#!/usr/bin/env bash

cp cephadm.py __main__.py
zip ca.zip cephadm.py
echo '#!/usr/bin/env python3' | cat - ca.zip > ca
chmod +x ca

