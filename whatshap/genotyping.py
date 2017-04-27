#!/usr/bin/env python3
"""
Runs only the genotyping algorithm. Genotype Likelihoods are computed using the
forward backward algorithm. 
"""
import logging
import sys
import platform
import resource
import math
from collections import defaultdict
from copy import deepcopy

import pyfaidx
from xopen import xopen

from contextlib import ExitStack
from .vcf import VcfReader, GenotypeVcfWriter, GenotypeLikelihoods
from . import __version__
from .core import ReadSet, readselection, Pedigree, PedigreeDPTable, NumericSampleIds, PhredGenotypeLikelihoods, GenotypeDPTable, compute_genotypes
from .graph import ComponentFinder
from .pedigree import (PedReader, mendelian_conflict, recombination_cost_map,
                       load_genetic_map, uniform_recombination_map, find_recombination)
from .bam import BamIndexingError, SampleNotFoundError, ReferenceNotFoundError
from .timer import StageTimer
from .variants import ReadSetReader, ReadSetError

from .phase import read_reads, select_reads, split_input_file_list, setup_pedigree


logger = logging.getLogger(__name__)

# given genotype likelihoods for 0/0,0/1,1/1, determines likeliest genotype
def determine_genotype(likelihoods, genotype_threshold):
	max_ind = -1
	max_val = -1
	
	threshold_prob = 1.0-(10 ** (-genotype_threshold/10.0))
	
	for i in range(len(likelihoods)):
		if likelihoods[i] > max_val:
			max_val = likelihoods[i]
			max_ind = i
			
	# in case likeliest gt has prob smaller than given threshold
	# we refuse to give a prediction (gt ./.)
	if max_val > threshold_prob:
		return max_ind
	else:
		return -1
	
def run_genotyping(phase_input_files, variant_file, reference=None,
		output=sys.stdout, samples=None, chromosomes=None,
		ignore_read_groups=False, indels=True, mapping_quality=20,
		max_coverage=15,
		ped=None, recombrate=1.26, genmap=None,
		gl_regularizer=None, gtchange_list_filename=None, gt_qual_threshold=15):
	"""
	For now: this function only runs the genotyping algorithm. Genotype likelihoods for
	all variants are computed using the forward backward algorithm
	"""
	print('running only genotyping algorithm')
	timers = StageTimer()
	timers.start('overall')
	logger.info("This is WhatsHap (genotyping) %s running under Python %s", __version__, platform.python_version())
	with ExitStack() as stack:
		
		# read the given input files (bams,vcfs,ref...)
		numeric_sample_ids = NumericSampleIds()
		phase_input_bam_filenames, phase_input_vcf_filenames = split_input_file_list(phase_input_files)
		try:
			readset_reader = stack.enter_context(ReadSetReader(phase_input_bam_filenames, numeric_sample_ids, mapq_threshold=mapping_quality))
		except (OSError, BamIndexingError) as e:
			logger.error(e)
			sys.exit(1)
		try:
			phase_input_vcf_readers = [VcfReader(f, indels=indels, phases=True) for f in phase_input_vcf_filenames]
		except OSError as e:
			logger.error(e)
			sys.exit(1)
		if reference:
			try:
				fasta = stack.enter_context(pyfaidx.Fasta(reference, as_raw=True))
			except OSError as e:
				logger.error('%s', e)
				sys.exit(1)
		else:
			fasta = None
		del reference
		if isinstance(output, str):
			output = stack.enter_context(xopen(output, 'w'))
		command_line = '(whatshap {}) {}'.format(__version__ , ' '.join(sys.argv[1:]))
		vcf_writer = GenotypeVcfWriter(command_line=command_line, in_path=variant_file,
		        out_file=output)
		
		# parse vcf
		# No genotype likelihoods may be given, therefore don't read them
		vcf_reader = VcfReader(variant_file, indels=indels, genotype_likelihoods=False)
		
		if ignore_read_groups and not samples and len(vcf_reader.samples) > 1:
			logger.error('When using --ignore-read-groups on a VCF with '
				'multiple samples, --sample must also be used.')
			sys.exit(1)
		if not samples:
			samples = vcf_reader.samples
		vcf_sample_set = set(vcf_reader.samples)
		for sample in samples:
			if sample not in vcf_sample_set:
				logger.error('Sample %r requested on command-line not found in VCF', sample)
				sys.exit(1)

		samples = frozenset(samples)
		# list of all trios across all families
		all_trios = dict()

		# Keep track of connected components (aka families) in the pedigree
		family_finder = ComponentFinder(samples)

		# if pedigree information present, parse it
		if ped:
			all_trios, pedigree_samples = setup_pedigree(ped, numeric_sample_ids, vcf_reader.samples)
			if genmap:
				logger.info('Using region-specific recombination rates from genetic map %s.', genmap)
			else:
				logger.info('Using uniform recombination rate of %g cM/Mb.', recombrate)
			for trio in all_trios:
				family_finder.merge(trio.mother, trio.child)
				family_finder.merge(trio.father, trio.child)

		# map family representatives to lists of family members
		families = defaultdict(list)
		for sample in samples:
			families[family_finder.find(sample)].append(sample)
		# map family representatives to lists of trios for this family
		family_trios = defaultdict(list)
		for trio in all_trios:
			family_trios[family_finder.find(trio.child)].append(trio)
		largest_trio_count = max([0] + [len(trio_list) for trio_list in family_trios.values()])
		logger.info('Working on %d samples from %d famil%s', len(samples), len(families), 'y' if len(families)==1 else 'ies')

		if max_coverage + 2 * largest_trio_count > 25:
			logger.warning('The maximum coverage is too high! '
				'WhatsHap may take a long time to finish and require a huge amount of memory.')

		# Read phase information provided as VCF files, if provided.
		phase_input_vcfs = []
		timers.start('parse_phasing_vcfs')
		for reader, filename in zip(phase_input_vcf_readers, phase_input_vcf_filenames):
			# create dict mapping chromsome names to VariantTables
			m = dict()
			logger.info('Reading phased blocks from %r', filename)
			for variant_table in reader:
				m[variant_table.chromosome] = variant_table
			phase_input_vcfs.append(m)
		timers.stop('parse_phasing_vcfs')

		timers.start('parse_vcf')
		for variant_table in vcf_reader:
			chromosome = variant_table.chromosome
			timers.stop('parse_vcf')
			if (not chromosomes) or (chromosome in chromosomes):
				logger.info('======== Working on chromosome %r', chromosome)
			else:
				logger.info('Leaving chromosome %r unchanged (present in VCF but not requested by option --chromosome)', chromosome)
				continue

			# Iterate over all families to process, i.e. a separate DP table is created
			# for each family.
			for representative_sample, family in sorted(families.items()):
				if len(family) == 1:
					logger.info('---- Processing individual %s', representative_sample)
				else:
					logger.info('---- Processing family with individuals: %s', ','.join(family))
				max_coverage_per_sample = max(1, max_coverage // len(family))
				logger.info('Using maximum coverage per sample of %dX', max_coverage_per_sample)
				trios = family_trios[representative_sample]

				assert (len(family) == 1) or (len(trios) > 0)

				# Get the reads belonging to each sample
				readsets = dict() 
				for sample in family:
					with timers('read_bam'):
						bam_sample = None if ignore_read_groups else sample
						readset = read_reads(readset_reader, chromosome, variant_table.variants, bam_sample, fasta, phase_input_vcfs, numeric_sample_ids, phase_input_bam_filenames)

					with timers('select'):
						selected_reads = select_reads(readset, max_coverage_per_sample)
					readsets[sample] = selected_reads

				# Merge reads into one ReadSet (note that each Read object
				# knows the sample it originated from).
				all_reads = ReadSet()
				for sample, readset in readsets.items():
					for read in readset:
						assert read.is_sorted(), "Add a read.sort() here"
						all_reads.add(read)

				all_reads.sort()

				# Determine which variants can (in principle) be phased
				accessible_positions = sorted(all_reads.get_positions())
				logger.info('Variants covered by at least one phase-informative '
					'read in at least one individual after read selection: %d',
					len(accessible_positions))

				# Keep only accessible positions
				phasable_variant_table = deepcopy(variant_table)
				phasable_variant_table.subset_rows_by_position(accessible_positions)
				assert len(phasable_variant_table.variants) == len(accessible_positions)

				# Create Pedigree
				pedigree = Pedigree(numeric_sample_ids)
				for sample in family:
					# genotypes are assumed to be unknown, so ignore information that
					# might be present in the input vcf
					pedigree.add_individual(sample, [0] * len(accessible_positions), None)
				for trio in trios:
					pedigree.add_relationship(
						mother_id=trio.mother,
						father_id=trio.father,
						child_id=trio.child)

				if genmap:
					# Load genetic map
					recombination_costs = recombination_cost_map(load_genetic_map(genmap), accessible_positions)
				else:
					recombination_costs = uniform_recombination_map(recombrate, accessible_positions)
					
				print("len acc pos: ", len(accessible_positions))

				# Finally, run genotyping algorithm
				with timers('genotyping'):
					problem_name = 'genotyping'
					logger.info('Genotype %d sample%s by solving the %s problem ...',
						len(family), 's' if len(family) > 1 else '', problem_name)
					forward_backward_table = GenotypeDPTable(numeric_sample_ids, all_reads, recombination_costs, pedigree, accessible_positions)
					# store results
					for s in family:
						
						# all genotypes/likelihoods to be stored (including non-accessible positions)
						likelihood_list = []
						genotypes_list = []
						
						for pos in range(len(accessible_positions)):
							likelihoods = forward_backward_table.get_genotype_likelihoods(s,pos)
							
							# compute genotypes from likelihoods
							geno = determine_genotype(likelihoods, gt_qual_threshold)
							genotypes_list.append(geno)
							
							# translate into phred scores
							likelihood_list.append(likelihoods)
							
							# just for testing: this only prints the results on command line ...
							print(s, accessible_positions[pos], likelihoods, geno)
							
						phasable_variant_table.set_genotypes_of(s, genotypes_list)
						phasable_variant_table.set_genotype_likelihoods_of(s,likelihood_list)
							
			# just for testing: print stored values
			#print(phasable_variant_table.genotypes_of(sample))
			#print(phasable_variant_table.genotype_likelihoods_of(sample))
			#print(phasable_variant_table.variants, len(phasable_variant_table.variants), len(accessible_positions))

			with timers('write_vcf'):
				logger.info('======== Writing VCF')
				vcf_writer.write_genotypes(chromosome,phasable_variant_table)
				logger.info('Done writing VCF')

			logger.debug('Chromosome %r finished', chromosome)
			timers.start('parse_vcf')
		timers.stop('parse_vcf')


	logger.info('\n== SUMMARY ==')
	timers.stop('overall')
	if sys.platform == 'linux':
		memory_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
		logger.info('Maximum memory usage: %.3f GB', memory_kb / 1E6)
	logger.info('Time spent reading BAM:                      %6.1f s', timers.elapsed('read_bam'))
	logger.info('Time spent parsing VCF:                      %6.1f s', timers.elapsed('parse_vcf'))
	if len(phase_input_vcfs) > 0:
		logger.info('Time spent parsing input phasings from VCFs: %6.1f s', timers.elapsed('parse_phasing_vcfs'))
	logger.info('Time spent selecting reads:                  %6.1f s', timers.elapsed('select'))
	logger.info('Time spent genotyping:                          %6.1f s', timers.elapsed('genotyping'))
	logger.info('Time spent writing VCF:                      %6.1f s', timers.elapsed('write_vcf'))
	logger.info('Time spent on rest:                          %6.1f s', 2 * timers.elapsed('overall') - timers.total())
	logger.info('Total elapsed time:                          %6.1f s', timers.elapsed('overall'))


	
def add_arguments(parser):
	arg = parser.add_argument
	# Positional arguments
	arg('variant_file', metavar='VCF', help='VCF file with variants to be genotyped (can be gzip-compressed)')
	arg('phase_input_files', nargs='*', metavar='PHASEINPUT',
	    help='BAM or VCF file(s) with phase information, either through sequencing reads (BAM) or through phased blocks (VCF)')

	arg('--version', action='version', version=__version__)
	arg('-o', '--output', default=sys.stdout,
		help='Output VCF file. Add .gz to the file name to get compressed output. '
			'If omitted, use standard output.')
	arg('--reference', '-r', metavar='FASTA',
		help='Reference file. Provide this to detect alleles through re-alignment. '
			'If no index (.fai) exists, it will be created')

	arg = parser.add_argument_group('Input pre-processing, selection and filtering').add_argument
	arg('--max-coverage', '-H', metavar='MAXCOV', default=15, type=int,
		help='Reduce coverage to at most MAXCOV (default: %(default)s).')
	arg('--mapping-quality', '--mapq', metavar='QUAL',
		default=20, type=int, help='Minimum mapping quality (default: %(default)s)')
	arg('--indels', dest='indels', default=False, action='store_true',
		help='Also genotype indels (default: do not genotype indels)')
	arg('--ignore-read-groups', default=False, action='store_true',
		help='Ignore read groups in BAM header and assume all reads come '
		'from the same sample.')
	arg('--sample', dest='samples', metavar='SAMPLE', default=[], action='append',
		help='Name of a sample to genotype. If not given, all samples in the '
		'input VCF are genotyped. Can be used multiple times.')
	arg('--chromosome', dest='chromosomes', metavar='CHROMOSOME', default=[], action='append',
		help='Name of chromosome to genotyped. If not given, all chromosomes in the '
		'input VCF are genotyped. Can be used multiple times.')
	arg('--gt-qual-threshold', metavar='GTQUALTHRESHOLD', type=float, default=15,
		help='Phred scaled error probability threshold used for genotyping (default: 15). '
		'If error probability of genotype is higher, genotype ./. is output.')
	arg = parser.add_argument_group('Pedigree genotyping').add_argument
	arg('--ped', metavar='PED/FAM',
		help='Use pedigree information in PED file to improve phasing '
		'(switches to PedMEC algorithm). Columns 2, 3, 4 must refer to child, '
		'mother, and father sample names as used in the VCF and BAM. Other '
		'columns are ignored.')
	arg('--recombrate', metavar='RECOMBRATE', type=float, default=1.26,
		help='Recombination rate in cM/Mb (used with --ped). If given, a constant recombination '
		'rate is assumed (default: %(default)gcM/Mb).')
	arg('--genmap', metavar='FILE',
		help='File with genetic map (used with --ped) to be used instead of constant recombination '
		'rate, i.e. overrides option --recombrate.')


def validate(args, parser):
	if args.ignore_read_groups and args.ped:
		parser.error('Option --ignore-read-groups cannot be used together with --ped')
	if args.genmap and not args.ped:
		parser.error('Option --genmap can only be used together with --ped')
	if args.genmap and (len(args.chromosomes) != 1):
		parser.error('Option --genmap can only be used when working on exactly one chromosome (use --chromosome)')
	if args.ped and args.samples:
		parser.error('Option --sample cannot be used together with --ped')
	if len(args.phase_input_files) == 0 and not args.ped:
		parser.error('Not providing any PHASEINPUT files only allowed in --ped mode.')


def main(args):
	run_genotyping(**vars(args))