apt update
apt install wget -y

wget https://github.com/qdrant/qdrant/releases/latest/download/qdrant-x86_64-unknown-linux-gnu.tar.gz
tar -xzf qdrant-x86_64-unknown-linux-gnu.tar.gz

mv qdrant /usr/local/bin/
chmod +x /usr/local/bin/qdrant

