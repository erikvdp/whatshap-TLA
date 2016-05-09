#include <iostream>
#include <fstream>

#include <cereal/archives/binary.hpp>

#include "readset.h"

using namespace std;

void usage(const char* name) {
	cerr << "Usage: " << name << " [options] <whatshap.instance>" << endl;
	cerr << endl;
	cerr << "De-serializes and runs a WhatsHap problem instance." << endl;
	exit(1);
}

int main(int argc, char* argv[]) {

	ifstream ifs;
	ifs.open("test.ser");
	cereal::BinaryInputArchive iarchive(ifs);
	ReadSet readset;
	iarchive(readset);
	cout << readset.toString() << endl;
	ifs.close();

	return 0;
}