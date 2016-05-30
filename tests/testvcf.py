from nose.tools import raises
from whatshap.vcf import VcfReader, MixedPhasingError, VariantCallPhase, VcfVariant


def test_read_phased():
	tables = list(VcfReader('tests/data/phasedinput.vcf'))
	assert len(tables) == 1
	table = tables[0]
	assert table.chromosome == 'ref'
	assert table.samples == ['sample']
	assert len(table.variants) == 2
	assert table.variants[0].reference_allele == 'A'
	assert table.variants[0].alternative_allele == 'C'
	assert table.variants[1].reference_allele == 'G'
	assert table.variants[1].alternative_allele == 'T'
	assert table.genotypes[0][0] == table.genotypes[0][1] == 1


def test_read_multisample_vcf():
	tables = list(VcfReader('tests/data/multisample.vcf'))
	assert len(tables) == 2
	table, table_b = tables
	assert table_b.chromosome == 'chrB'
	assert table_b.samples == ['sample1', 'sample2']

	assert table.chromosome == 'chrA'
	assert len(table.variants) == 3
	assert table.samples == ['sample1', 'sample2']

	assert table.variants[0].reference_allele == 'A'
	assert table.variants[0].alternative_allele == 'T'
	assert table.variants[1].reference_allele == 'C'
	assert table.variants[1].alternative_allele == 'G'
	assert table.variants[2].reference_allele == 'G'
	assert table.variants[2].alternative_allele == 'T'

	assert len(table.genotypes) == 2
	assert list(table.genotypes[0]) == [1, 1, 1]
	assert list(table.genotypes[1]) == [1, 1, 0]

	assert list(table.genotypes_of('sample1')) == [1, 1, 1]
	assert list(table.genotypes_of('sample2')) == [1, 1, 0]


def test_read_phased_vcf():
	for filename in ['tests/data/phased-via-HP.vcf', 'tests/data/phased-via-PS.vcf']:
		print('Testing', filename)
		tables = list(VcfReader(filename))
		assert len(tables) == 2
		table_a, table_b = tables

		assert table_a.chromosome == 'chrA'
		assert len(table_a.variants) == 4
		assert table_a.samples == ['sample1', 'sample2']

		assert table_b.chromosome == 'chrB'
		assert len(table_b.variants) == 2
		assert table_b.samples == ['sample1', 'sample2']

		assert len(table_a.genotypes) == 2
		assert list(table_a.genotypes[0]) == [1, 2, 1, 1]
		assert list(table_a.genotypes[1]) == [1, 1, 1, 1]
		assert list(table_a.genotypes_of('sample1')) == [1, 2, 1, 1]
		assert list(table_a.genotypes_of('sample2')) == [1, 1, 1, 1]

		assert len(table_b.genotypes) == 2
		assert list(table_b.genotypes[0]) == [0, 1]
		assert list(table_b.genotypes[1]) == [1, 2]
		assert list(table_b.genotypes_of('sample1')) == [0, 1]
		assert list(table_b.genotypes_of('sample2')) == [1, 2]

		print(table_a.phases)
		assert len(table_a.phases) == 2
		expected_phase_sample1 = [
			None, 
			None, 
			VariantCallPhase(block_id=300, phase=1, quality=23),
			VariantCallPhase(block_id=300, phase=0, quality=42)
		]
		expected_phase_sample2 = [
			VariantCallPhase(block_id=100, phase=0, quality=10),
			VariantCallPhase(block_id=100, phase=1, quality=20),
			VariantCallPhase(block_id=300, phase=0, quality=30),
			VariantCallPhase(block_id=300, phase=0, quality=None)
		]
		assert list(table_a.phases[0]) == expected_phase_sample1
		assert list(table_a.phases[1]) == expected_phase_sample2
		assert list(table_a.phases_of('sample1')) == expected_phase_sample1
		assert list(table_a.phases_of('sample2')) == expected_phase_sample2

		assert len(table_b.phases) == 2
		assert list(table_b.phases[0]) == [None, None]
		assert list(table_b.phases[1]) == [None, None]
		assert list(table_b.phases_of('sample1')) == [None, None]
		assert list(table_b.phases_of('sample2')) == [None, None]


@raises(MixedPhasingError)
def test_mixed_phasing_vcf():
	tables = list(VcfReader('tests/data/phased-via-mixed-HP-PS.vcf'))


def test_vcf_variant_hashability():
	v = [
		VcfVariant(10, 'A', 'TC'),
		VcfVariant(10, 'A', 'TCA'),
		VcfVariant(10, 'C', 'TC'),
		VcfVariant(20, 'A', 'TC'),
		VcfVariant(10, 'A', 'TCA'),
		VcfVariant(20, 'A', 'TC')
	]
	assert len(set(v)) == 4


def test_phasing_to_reads():
	for filename in ['tests/data/phased-via-HP.vcf', 'tests/data/phased-via-PS.vcf']:
		tables = list(VcfReader(filename))
		assert len(tables) == 2
		table_a, table_b = tables
		phase_reads_sample1 = list(table_a.phased_blocks_as_reads('sample1', table_a.variants, 17, 18, default_quality=90, mapq=101))
		print(phase_reads_sample1)
		assert len(phase_reads_sample1) == 1
		read = phase_reads_sample1[0]
		assert len(read) == 2
		assert read.name == 'block300'
		assert read.source_id == 17
		assert read.mapqs == (101,)
		assert read[0].position == 300 - 1
		assert read[0].allele == 1
		assert read[0].quality == 23
		assert read[1].position == 350 - 1
		assert read[1].allele == 0
		assert read[1].quality == 42

		phase_reads_sample2 = list(table_a.phased_blocks_as_reads('sample2', table_a.variants, 11, 12, default_quality=91, mapq=102))
		print(phase_reads_sample2)
		assert len(phase_reads_sample2) == 2
		read1, read2 = phase_reads_sample2
		assert len(read1) == len(read2) == 2
		if read1[0].position > read2[0].position:
			read1, read2 = read2, read1
		assert read1.name == 'block100'
		assert read1.source_id == 11
		assert read1.mapqs == (102,)
		assert read1[0].position == 100 - 1
		assert read1[0].allele == 0
		assert read1[0].quality == 10
		assert read1[1].position == 150 - 1
		assert read1[1].allele == 1
		assert read1[1].quality == 20
		assert read2.name == 'block300'
		assert read2.source_id == 11
		assert read2.mapqs == (102,)
		assert read2[0].position == 300 - 1
		assert read2[0].allele == 0
		assert read2[0].quality == 30
		assert read2[1].position == 350 - 1
		assert read2[1].allele == 0
		assert read2[1].quality == 91

		variants = [
			VcfVariant(350 - 1, 'G', 'T'),
			VcfVariant(300 - 1, 'G', 'T'),
			VcfVariant(17, 'A', 'TTC'),
			VcfVariant(1000, 'C', 'G')
		]
		phase_reads_sample2 = list(table_a.phased_blocks_as_reads('sample2', variants, 11, 12, default_quality=91, mapq=102))
		print(phase_reads_sample2)
		assert len(phase_reads_sample2) == 1
		read = phase_reads_sample2[0]
		assert len(read) == 2
		assert read.name == 'block300'
		assert read.source_id == 11
		assert read.mapqs == (102,)
		assert read[0].position == 300 - 1
		assert read[0].allele == 0
		assert read[0].quality == 30
		assert read[1].position == 350 - 1
		assert read[1].allele == 0
		assert read[1].quality == 91
