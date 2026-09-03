# Teams activity worker

Python Temporal activity worker that renders the Go tunnel's lossless A2A pages in
Microsoft Teams. It performs all Microsoft Teams API calls outside workflow code.

Each process polls the shared Teams delivery queue for new streams and a private
task queue for streams it owns. The delivery activity returns that private queue to
the tunnel as opaque driver state, keeping Microsoft Teams SDK stream state
process-local while allowing multiple Python workers to run safely.
