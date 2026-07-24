# Teams activity worker

Python Temporal activity worker to connect Microsoft Teams messaging to Temporal. It
shares the Teams connector task queue with the Go workflow worker and performs
all Microsoft Teams API calls outside workflow code.

Each process polls the shared connector queue for new streams and a private task
queue for updates to streams it owns. This keeps the Microsoft Teams SDK stream
state process-local while allowing multiple Python worker processes to run safely.
