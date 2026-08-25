"""gnrprobe — what a GenroPy request actually costs.

Two recorders and one archive. The HTTP recorder writes one line per exchange,
the register recorder one line per call the site makes to its site register, and
a request header joins the two, so every register call is attributable to the
exchange that caused it. A reading layer turns that into the questions worth
asking: which RPC costs the most register calls, where the time goes when it is
neither SQL nor XML, and which failures the register's own retry loop swallowed.
"""

VERSION = "0.1.0"
