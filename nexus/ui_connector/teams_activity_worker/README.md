# Teams activity worker

Python Temporal activity worker to connect Microsoft Teams messaging to Temporal. It
shares the Teams connector task queue with the Go workflow worker and performs
all Microsoft Teams API calls outside workflow code.
