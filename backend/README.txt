Backend AirDrop nativo per Ubuntu 26.10 amd64

I binari OWL e OpenDrop sono usati dall'interfaccia dell'app.
Non avviare OWL direttamente: le interfacce Wi-Fi richiedono il helper
privilegiato e un ripristino della rete. OpenDrop gira come utente normale.

./opendrop --help mostra il CLI completo; ./owl non cambia la rete quando
nessuna interfaccia viene specificata. Il launcher OpenDrop carica le
protezioni secure_receiver.py incluse nella directory superiore.

Runtime: /usr/bin/python3, ABI CPython 3.14, libarchive.so.13; OWL usa
libpcap.so.0.8 e libnl-{3,genl-3,route-3}.so.200 dal sistema. libev 4.33
e' collegata staticamente. Nessuna dipendenza viene installata nel sistema.

Versioni e pin esatti: manifest.json. Hash wheel: wheels.lock.json.
Licenze: licenses/ e python/*dist-info/. Sorgenti OWL e libev: sources/.
Le diciassette patch OpenDrop sono conservate in sources/opendrop-patches.

Compilazione OWL: GCC 15.3.0, CMake 4.3.4, Release, -fno-strict-aliasing,
CMAKE_POLICY_VERSION_MINIMUM=3.5, CMAKE_SKIP_RPATH=TRUE, -lm.
Patch locale owl-gcc15-safety.patch: inizializza puntatori di cleanup
Netlink e metadati radiotap mancanti, verifica chdir durante daemonize.
I 65 test unita' upstream passano; il googletest incluso richiede
CMAKE_CXX_FLAGS='-include cstdint -Wno-error=maybe-uninitialized'.

Compatibilita' radio e iOS 27 non provata con un trasferimento reale.
Il fork upstream documenta prove su MT7921/MT7922 e iOS 26.
