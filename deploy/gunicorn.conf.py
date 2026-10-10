# Web workers are stateless: printer connections live in the controller daemon,
# so multiple workers are safe.
bind = "0.0.0.0:8092"
workers = 2
threads = 4
worker_class = "gthread"
timeout = 60
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = "127.0.0.1"
