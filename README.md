# gluetun-watcher
A docker container that will restart certain containers after a specific container is restarted

# My configuration
Currently, my configuration for gluetun is for my arr stack, where certain linux isos are "found on the internet". Using gluetun, I am able to network certain contianer's traffic into vpn traffic while maintaining control over the local functions of the arr stack, such as webui through traefik and such. I needed this container because when gluetun restarts, the network id of the gluetun docker container changes, rendering the arr stack docker network ids useless until I restart them to pull the new docker network id of gluetun.
