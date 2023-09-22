#!/usr/bin/env python3
import subprocess
import sys
import os
import io
import gzip
import time
from argparse import ArgumentParser
from itertools import combinations_with_replacement
import numpy as np
import pandas as pd
from icecream import ic
from Bio.SeqIO.FastaIO import SimpleFastaParser
from Bio import SeqIO
import networkx as nx
from tqdm import tqdm
from datetime import timedelta
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap, BoundaryNorm
import wget
import shutil
import glob
import scipy.cluster.hierarchy as sch
import matplotlib.colors as mcolors

# Set matplotlib parameters
import matplotlib.pyplot as plt

plt.rcParams["text.color"] = "#131516"
plt.rcParams["svg.fonttype"] = "none"  # Editable SVG text
plt.rcParams["font.family"] = "Arial"
plt.rcParams["font.weight"] = "light"

def print_error(txt): print(f"\033[31m{txt}\033[0m")
def print_warn(txt): print(f"\033[94m{txt}\033[0m")
def print_ok(txt): print(f"\033[34m{txt}\033[0m")
def print_res(txt): print(f"\033[33m{txt}\033[0m")

class PoorMansViridic:
    def __init__(self, file, genus_threshold=70, species_threshold=95, nthreads=1, verbose = True):
        self.verbose = verbose
        self.file = file
        self.result_dir = os.path.dirname(self.file)
        self.nthreads = nthreads
        self.genus_threshold = genus_threshold
        self.species_threshold = species_threshold

    def run(self):
        print(f"Running PoorMansViridic on {self.file}\n")
        self.makeblastdb()
        self.blastn()
        self.parse_blastn_file()
        self.calculate_distances()
        self.cluster_all()
        return self.dfT, self.pmv_outfile

    def cluster_all(self):
        dfTg = self.sim2cluster(self.genus_threshold, 'genus')
        dfTs = self.sim2cluster(self.species_threshold, 'species')
        dfT = pd.merge(dfTg, dfTs, on='genome').sort_values('species_cluster genus_cluster'.split())
        dfT.reset_index(drop=True, inplace=True)
        self.pmv_outfile = os.path.join(self.result_dir, os.path.basename(self.file) + '.genus_species_clusters.tsv')
        dfT.to_csv(self.pmv_outfile, index=False, sep='\t')
        self.dfT = dfT

    def sim2cluster(self, th, tax_level):
        ic("Generating graph for finding", tax_level, "clusters")
        M = self.dfM
        G = nx.from_pandas_edgelist(M[(M.sim >= th) & (M.A != M.B)], source='A', target='B')
        singletons = list(set(M.A.unique().tolist()).difference(G.nodes()))
        G.add_nodes_from(singletons)

        graphs = [G.subgraph(x) for x in nx.connected_components(G)]
        L = []
        for n, g in enumerate(graphs):
            L.extend([(node, n+1) for node in g.nodes()])

        return pd.DataFrame(L, columns=f'genome {tax_level}_cluster'.split())

    def makeblastdb(self):
        # Find all the files created by makeblastdb and remove them
        for filename in glob.glob(f"{self.file}*.n*"):
                    os.remove(filename)

        cmd = f'makeblastdb -in {self.file}  -dbtype nucl'
        ic("Creating blastn database:", cmd)
        res = subprocess.getoutput(cmd)
        ic(res)
        
        
    def blastn(self):
        outfile = os.path.join(self.result_dir, os.path.basename(self.file) + '.blastn_vs2_self.tab.gz')
        if not os.path.exists(outfile):
            cmd = f'blastn -evalue 1 -max_target_seqs 10000 -num_threads {self.nthreads} -word_size 7 -reward 2 -penalty -3 -gapopen 5 -gapextend 2 -query {self.file} -db {self.file} -outfmt "6 qseqid sseqid pident length qlen slen mismatch nident gapopen qstart qend sstart send qseq sseq evalue bitscore" | gzip -c > {outfile}'
            ic("Blasting against itself:", cmd)
            ic(cmd)
            subprocess.getoutput(cmd)

        self.blastn_result_file = outfile

    def parse_blastn_file(self):
        ic("Reading", self.blastn_result_file)

        num_lines = rawgencount(self.blastn_result_file)

        self.size_dict = {}
        M = {}
        with gzip.open(self.blastn_result_file, 'rt') as df:
            for line in tqdm(df, total=num_lines):
                # do something with the line
                qseqid, sseqid, pident, length, qlen, slen, mismatch, nident, gapopen, qstart, qend, sstart, send, qseq, sseq, evalue, bitscore = line.rstrip().split()
                key = (qseqid, sseqid)
                M.setdefault(key, np.zeros(int(qlen)))

                if qseqid not in self.size_dict:
                    self.size_dict[qseqid] = int(qlen) 
                if sseqid not in self.size_dict:
                    self.size_dict[sseqid] = int(slen)

                # convert the strings to numpy arrays
                qseq = np.frombuffer(qseq.encode('utf-8'), dtype="S1")
                sseq = np.frombuffer(sseq.encode('utf-8'), dtype="S1")

                v = np.where(qseq == sseq, 1, 0)

                # find the indices of elements that are not equal to '-'. Here it is b'-' because the array is of type bytes.
                idx = qseq != b'-'

                # add the values to the matrix
                M[key][int(qstart)-1:int(qend)] += v[idx]

        # convert the matrix to a binary matrix
        M = {key:np.where(value != 0, 1, 0) for key, value in M.items()}

        self.M = M

    def calculate_distances(self):
        M = self.M
        size_dict = self.size_dict

        genome_arr = np.array(list(M.keys()))
        identity_arr = np.array(list(M.values()), dtype=object).reshape(-1, 1)
        dfM = pd.DataFrame(np.hstack([genome_arr, identity_arr]), columns=['A', 'B', 'identity_seq'])

        dfM["idAB"] = dfM['identity_seq'].apply(lambda x: np.sum(x))

        # creating a dictionary of genome name identity 
        # As the blast is double sided need to check the identity of both genomes by looking at the opposite pair
        dict_BA = dfM.set_index(["A", "B"]).idAB.to_dict()

        # Creating the pair of genomes in order B, A
        dfM["pair_BA"] = dfM.apply(lambda x: (x.A, x.B), axis=1)

        # Setting the identity of the pair B, A
        dfM["idBA"] = dfM.pair_BA.map(dict_BA)

        # If the identity of the pair B, A is NaN then the pair is A, B
        dfM.loc[dfM.idBA.isna(), "idBA"] = dfM.loc[dfM.idBA.isna(), "idAB"]

        dfM["lA"] = dfM['A'].map(size_dict)
        dfM["lB"] = dfM['B'].map(size_dict)

        dfM["simAB"] = ((dfM.idAB + dfM.idBA) * 100) / (dfM.lA + dfM.lB)

        dfM["distAB"] = 100 - dfM.simAB

        dfM["aligned_fraction_genome_1"] = dfM.idAB / dfM.lA
        dfM["aligned_fraction_genome_2"] = dfM.idBA / dfM.lB
        dfM["genome_length_ratio"] = dfM[["lA", "lB"]].min(axis=1) / dfM[["lA", "lB"]].max(axis=1)

        dfM['sim'] = 100 - dfM.distAB

        # Remove the duplicate pairs
        dfM["ordered_pair"] = dfM.apply(lambda x: str(sorted(x.pair_BA)), axis=1)
        dfM = dfM.drop_duplicates("ordered_pair").reset_index(drop=True)

        dfM = dfM.drop(columns=['identity_seq', 'pair_BA', 'idAB', 'idBA', 'lA', 'lB', 'simAB'])

        self.dfM = dfM

    def save_similarities(self, outfile='similarities.tsv'):
        df = self.dfM[['A', 'B', 'sim']]
        df = df[df.A != df.B]
        df.sort_values('sim', ascending=False, inplace=True)
        df.index.name = ''
        df.to_csv(outfile, index=False, sep='\t')
        self.dfM.sort_values('sim', ascending=False).to_csv(outfile+".dfM.tsv", index=False, sep='\t')


def _make_gen(reader):
    """Generator to read a file piece by piece.
    Default chunk size: 1k.
    Args:
        reader (func): Function to read a piece of the file.
    Yields:
        generator: A generator object that yields pieces of the file.
    """
    b = reader(1024 * 1024)
    while b:
        yield b
        b = reader(1024*1024)

def rawgencount(filename):
    """Count the number of lines in a file.
    Args:
        filename (str): The name of the file to count.
    Returns:
        int: The number of lines in the file.
    """
    f = gzip.open(filename, 'rb') 
    f_gen = _make_gen(f.read)
    return sum( buf.count(b'\n') for buf in f_gen )


def heatmap(dfM, outfile, matrix_out, cmap='Greens'):

    #define output files
    svg_out = outfile+".svg"
    pdf_out = outfile+".pdf"
    jpg_out = outfile+".jpg"
    ax = plt.gca()
    dfM['A'] = dfM['A'].map(lambda x: x + ':' + accession_genus_dict.get(x, ''))
    dfM['B'] = dfM['B'].map(lambda x: x + ':' + accession_genus_dict.get(x, ''))
    dfM.update(dfM.loc[dfM.A > dfM.B].rename({'A': 'B', 'B': 'A'}, axis=1))
    dfM = dfM.round(2)
    df = dfM.pivot(index='A', columns='B', values='sim').fillna(0)
    df = df.rename({'taxmyPhage':'query'}, axis=1).rename({'taxmyPhage':'query'}, axis=0)

    # Make the matrix symmetric
    df = df + df.T - np.diag(df.values.diagonal())

    # Perform hierarchical clustering
    Z = sch.linkage(df, method='ward')

    # Plot the dendrogram
    dendrogram = sch.dendrogram(Z, labels=df.index, no_plot=True)

    # Looking for the query leave to put at the end
# leaves_order = []

# for leave in dendrogram['ivl']:
#     if "query" not in leave:
#         leaves_order.append(leave)
#     else:
#         query_leave = leave

# leaves_order.append(query_leave)

    # Reorder the matrix
    df = df.loc[leaves_order, leaves_order]
    df.iloc[:,:] = np.triu(df.values, k=0)
    # Maybe the following method is faster 
    # df = df.where(np.triu(np.ones(df.shape)).astype(np.bool))

    df.to_csv(matrix_out, sep='\t', index=True)

    colors = ["white", "lightgray", "skyblue", "steelblue", "darkgreen"]
    boundaries = [0, 1, 50, 70, 95, 100]

    norm = mcolors.BoundaryNorm(boundaries, len(colors))
    # Create the colormap
    custom_cmap = mcolors.ListedColormap(colors)

    #image
    #im = plt.imshow(df.values, cmap=cmap)
    im = plt.imshow(df.values, cmap=custom_cmap, norm=norm)

    ax.set_xticks(np.arange(df.shape[1]), labels=df.columns.tolist())
    ax.set_yticks(np.arange(df.shape[0]), labels=df.index.tolist())

    ax.tick_params(top=True, bottom=False, labeltop=True, labelbottom=False)

    plt.setp(ax.get_xticklabels(), rotation=-30, ha="right", rotation_mode="anchor")
    ax.spines[:].set_visible(False)

    ax.set_xticks(np.arange(df.shape[1]+1)-.5, minor=True)
    ax.set_yticks(np.arange(df.shape[0]+1)-.5, minor=True)
    ax.grid(which="minor", color="w", linestyle='-', linewidth=3)
    ax.tick_params(which="minor", bottom=False, left=False)
    
    fig_width = max(4, df.shape[1] * 0.75)
    fig_height = max(4, df.shape[0] * 0.75)
    plt.gcf().set_size_inches(fig_width, fig_height)

    for i in range(df.shape[0]):
        for j in range(df.shape[1]):
            font_size = (min(fig_width, fig_height) / max(df.shape[0], df.shape[1])) * 10

            ax.text(j, i, df.iloc[i, j], ha="center", va="center", color="w", fontsize=font_size)
    
    # adjust figure size based on number of rows and columns

    
    #plot with padding
    plt.tight_layout(pad=2.0)
    
    plt.savefig(svg_out)
    plt.savefig(pdf_out)
    plt.savefig(jpg_out)
    return im

        
def is_program_installed_unix(program_name):
    try:
        subprocess.check_output(f'which {program_name}', shell=True)
        return True
    except subprocess.CalledProcessError:
        return False

def check_programs():
    # check programs are installed
    program_name = "blastdbcmd"
    if is_program_installed_unix(program_name):
        ic(program_name, "is installed will proceed ")
    else:
        print_error(f"{program_name} is not installed.")
        sys.exit()

    program_name = "mash"
    if is_program_installed_unix(program_name):
        ic(program_name, "is installed will proceed ")
    else:
        print_error(f"{program_name} is not installed.")
        sys.exit()

def check_blastDB(blastdb_path):
    #check if blastDB is present
    if os.path.exists(blastdb_path):
        print_ok(f"Found {blastdb_path} as expected")
    else:
        print_error(f"File {blastdb_path} does not exist will create database now  ")
        print_error("Will download the database now and create database")
        url = "https://millardlab-inphared.s3.climb.ac.uk/Bacteriophage_genomes.fasta.gz"
        try:
            create_folder(os.path.dirname(blastdb_path))
            wget.download(url, f"{blastdb_path}.gz")
            print(f"\n{url} downloaded successfully!")
        except subprocess.CalledProcessError as e:
            print(f"An error occurred while downloading {url}: {e}")
        #Gunzip the file
        try:
            with gzip.open(f"{blastdb_path}.gz", 'rb') as f_in:
                with open(blastdb_path, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
                    os.remove(f"{blastdb_path}.gz")

            print("File gunzipped successfully!")
        except subprocess.CalledProcessError as e:
            print(f"An error occurred while gunzipping the file: {e}")

        # Run makeblastdb
        makeblastdb_command = f"makeblastdb -in {blastdb_path} -parse_seqids -dbtype nucl"
        try:
            subprocess.run(makeblastdb_command, shell=True, check=True)
            print("makeblastdb command executed successfully!")
        except subprocess.CalledProcessError as e:
            print(f"An error occurred while executing makeblastdb: {e}")

def create_folder(mypath):

    """
    Created the folder that I need to store my result if it doesn't exist
    :param mypath: path where I want the folder (write at the end of the path)
    :type: string
    :return: Nothing
    """

    try:
        os.makedirs(mypath)
    except OSError:
        pass

    return

    
if __name__ == '__main__':
    description = """Takes a phage genome as as fasta file and compares against all phage genomes that are currently classified 
         by the ICTV. It does not compare against ALL phage genomes, just classified genomes. Having found the closet related phages 
         it runs the VIRIDIC--algorithm and parses the output to predict the taxonomy of the phage. It is only able to classify to the Genus and Species level"""
    parser = ArgumentParser(description=description)
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
    )
    parser.add_argument(
        "-t",
        "--threads",
        dest="threads",
        type=str,
        default="8",
        help="Maximum number of threads that will be used",
    )
    parser.add_argument(
        "-i",
        "--input",
        dest="in_fasta",
        type=str,
        help="Path to an input fasta file",
        required=True,
    )
    parser.add_argument(
        "-db",
        "--database",
        dest="ICTV_db",
        type=str,
        help="Path to the database of genomes currently classified by the ICTV",
        default=os.path.abspath(os.path.join(os.path.expanduser('~'), ".taxmyPHAGE", "Bacteriophage_genomes.fasta")),
    )
    parser.add_argument(
        "--mash_index",
        dest="mash_index",
        type=str,
        help="Path to the prebuilt MASH index of ICTV genomes",
        default=os.path.abspath(os.path.join(os.path.expanduser('~'), ".taxmyPHAGE", "ICTV.msh")),
    )
    parser.add_argument(
        "--VMR",
        dest="VMR_file",
        type=str,
        help="Path to an input fasta file",
        default=os.path.abspath(os.path.join(os.path.expanduser('~'), ".taxmyPHAGE", "VMR.xlsx")),
    )
    parser.add_argument(
        "-p",
        "--prefix",
        type=str,
        default="",
        dest="prefix",
        help="will add the prefix to results and summary files that will store results of MASH and comparision to the VMR Data produced by"
        "ICTV combines both sets of this data into a single csv file. "
        "Use this flag if you want to run multiple times and keep the results files without manual renaming of files",
    )
    parser.add_argument(
        "-d",
        "--distance",
        type=str,
        default="0.2",
        dest="dist",
        help="Will change the mash distance for the intial seraching for close relatives. We suggesting keeping at 0.2"
        " If this results in the phage not being classified, then increasing to 0.3 might result in an output that shows"
        " the phage is a new genus. We have found increasing above 0.2 does not place the query in any current genus, only"
        " provides the output files to demonstrate it falls outside of current genera",
    )
    parser.add_argument(
        "--no-figures",
        dest="Figure",
        action="store_false",
        help="Use this option if you don't want to generate Figures. This will speed up the time it takes to run the script - but you get no Figures. ",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=os.path.join(os.getcwd(), f"taxmyphage_results"),
        dest="output",
        help="Path to the output directory",
    )

    args, nargs = parser.parse_known_args()
    verbose = args.verbose

    #turn on ICECREAM reporting
    if not verbose: ic.disable()

    timer_start = time.time()

    # this is the location of where the script and the databases are (instead of current_directory which is the users current directory)
    VMR_path = args.VMR_file
    blastdb_path = args.ICTV_db
    ICTV_path = args.mash_index

    print("Looking for database files...\n")

    if os.path.exists(VMR_path):
        print_ok(f"Found {VMR_path} as expected")
    else:
        print_error(f'File {VMR_path} does not exist will try downloading now')
        print_error("Will download the current VMR now")
        url = "https://ictv.global/vmr/current"
        try:
            create_folder(os.path.dirname(VMR_path))
            wget.download(url, VMR_path)
            print(f"\n{url} downloaded successfully!")
        except subprocess.CalledProcessError as e:
            print(f"An error occurred while downloading {url}: {e}")

    if os.path.exists(ICTV_path):
        print_ok(f"Found {ICTV_path} as expected")
    #else:
    #    print_error(f'File {ICTV_path} does not exist, was it downloaded correctly?')
    else:
        print_error(f"File {ICTV_path} does not exist will create database now  ")
        print_error("Will download the database now and create database")
        url = "https://millardlab-inphared.s3.climb.ac.uk/ICTV_2023.msh"
        try:
            create_folder(os.path.dirname(ICTV_path))
            wget.download(url, ICTV_path)
            print(f"\n{url} downloaded successfully!")
        except subprocess.CalledProcessError as e:
            print(f"An error occurred while downloading {url}: {e}")

    check_programs()
    check_blastDB(blastdb_path)
    
    #Defined and set some parameters
    fasta_file = args.in_fasta
    threads = args.threads
    mash_dist = args.dist
    ic("Number of set threads", threads)
    #create results folder
    results_path = args.output
    query = os.path.join(results_path,'query.fasta')

    #path to the combined df containing mash and VMR data
    out_csv_of_taxonomy = args.prefix+"Output_of_taxonomy.csv"
    taxa_csv_output_path = os.path.join(results_path, out_csv_of_taxonomy)

    #path the final results summary file
    summary_results = args.prefix+"Summary_file.txt"
    summary_output_path = os.path.join(results_path, summary_results)

    #fasta file to store known taxa
    known_taxa_path = os.path.join(results_path, 'known_taxa.fa')
    #store files for VIRIDIC run- or equivalent
    viridic_in_path = os.path.join(results_path, 'viridic_in.fa')

    heatmap_file = os.path.join(results_path, 'heatmap')
    top_right_matrix = os.path.join(results_path, 'top_right_matrix.tsv')
    similarities_file = os.path.join(results_path, 'similarities.tsv')
    #Statments to output

    summary_statement1 ="""
    \n The data from the initial mash searching is below as tsv format \n
    Remember taxmyPHAGE compared against viruses classified by the ICTV. Allowing you determine if it represents a new 
    species or genus. It does not tell you if it is similar to other phages that have yet to be classified 
    You can do this by comparison with INPHARED database if you wish https://github.com/RyanCook94/inphared or BLAST etc \n\n
    """

    statement_current_genus_new_sp="""
    Query sequence can be classified within a current genus and represents a new species, it is in:\n
    """
    statement_current_genus_sp="""
    \nQuery sequence can be classified within a current genus and species, it is in:\n
    """
    summary_statement_inconsitent="""
    The number of expected genera based on current ICTV classification is less than the predicted 
    number of genus clusters as predicted by VIRIDIC-algorithm. This does not mean the current ICTV classification
    is wrong (it might be)or that VIRIDIC-algorithm is wrong. It could be an edge case that automated process cannot
    distinguish. It will require more manual curation to look at the output files
    \n 
    """

    print("\nStarting tax_my_phage analysis...\n")

    #create the results folder
    create_folder(results_path)

    #write the input file to a new file with a header called "query_{id of the user}" which makes it easier to find in data later on
    handle = gzip.open(fasta_file, 'rt') if fasta_file.endswith('.gz') else open(fasta_file, "rt")

    num_sequences = handle.read().count('>')

    # Reset the file pointer to the beginning of the file
    handle.seek(0)

    if num_sequences == 0:
        print_error("Error: The FASTA file is empty.")
        sys.exit()
    elif num_sequences == 1:
        # Open output FASTA file
        with open(query, "w") as output_fid:
            parser = SeqIO.parse(handle, "fasta")
            for record in parser:
                record.name = record.description = ""
                record.id = f"query_{record.id}"
                SeqIO.write(record, output_fid, "fasta")
    else:
        print_error(f"\nError: The {fasta_file} FASTA file contains {num_sequences} sequences."\
              " Only one sequence can be classified at a time ")
        sys.exit()

    handle.close()

    #Read the viral master species record into a DataFrame
    taxa_df = pd.read_excel(VMR_path,sheet_name=0)

    # Print the DataFrame and rename a column
    ic(taxa_df.head())

    taxa_df = taxa_df.rename(columns={'Virus GENBANK accession': 'Genbank'})
    taxa_df['Genbank'].fillna('', inplace=True)
    # Get the column headings as a list

    #headings = list(taxa_df.columns)
    #create a dictionary of Accessions linking to Genus
    accession_genus_dict = taxa_df.set_index('Genbank')['Genus'].to_dict()

    #run mash to get top hit and read into a pandas dataframe
    cmd = f"mash dist -d {mash_dist} -p {threads} {ICTV_path} {query}"
    ic(cmd)
    mash_output = subprocess.getoutput(cmd)
    #mash_output = subprocess.check_output(['mash', 'dist', '-d', mash_dist, '-p', threads, ICTV_path, query])

    # list of names for the headers
    mash_df = pd.read_csv(io.StringIO(mash_output), sep='\t', header=None, names=['Reference', 'Query', 'distance', 'p-value', 'shared-hashes', 'ANI'])
    ic(ICTV_path)
    ic(mash_df)
    number_hits = mash_df.shape[0]

    #get the number of genomes wih mash distance < 0.2

    if number_hits < 1:
        print_error("""
    Error: No hits were found with the default settings
    The phage likely represents a new species and genus 
    However tax_my_phage is unable to classify it at this point as it can only classify at the Genus/Species level
              """)
        os.system(f"touch {taxa_csv_output_path}")
        sys.exit()
    else:
        print_res(f"""
        Number of phage genomes detected with mash distance of < {args.dist} is:{number_hits}""")

    #sort dataframe by distance so they are at the top
    mash_df = mash_df.sort_values(by='distance', ascending=True)
    mash_df.to_csv(os.path.join(results_path, 'mash.txt'), index=False)
    minimum_value = mash_df['distance'].min()
    maximum_value = mash_df.head(10)['distance'].max()

    print_ok(f"""\nThe mash distances obtained for this query phage
    is a minimum value of {minimum_value} and maximum value of {minimum_value}\n""")

    #set the maximum number of hits to take forward. Max is 50 or the max number in the table if <50
    filter_hits =""
    if number_hits < 10:
        filter_hits = number_hits
    else:
        filter_hits = 10

    #copy top 50 hits to a new dataframe
    top_50 = mash_df.iloc[:filter_hits].copy()

    ic(mash_df.head(10))
    ic(top_50)
    #reindex
    top_50.reset_index(drop=True, inplace=True)

    value_at_50th_position = top_50['distance'].iloc[filter_hits-1]
    ic(value_at_50th_position)
    
    top_50['genus'] = top_50['Reference'].str.split('/').str[1]
    top_50['acc'] = top_50['Reference'].str.split('/').str[-1].str.split('.fna|.fsa').str[0]
    top_50 = top_50.merge(taxa_df, left_on = 'acc', right_on = 'Genbank')
    top_50['ANI'] = (1 - top_50.distance)*100

    #returns the unique genera names found in the mash hits - top_50 is not the best name!

    unique_genera_counts = top_50.genus.value_counts()
    ic(unique_genera_counts.to_dict())
    unique_genera = unique_genera_counts.index.tolist()

    #unique_genera top_50.genus.value_counts().to_dict()
    #print for error checking
    ic(unique_genera)

    #number of genera
    number_of_genera = len(unique_genera)

    print_ok(f"Found {number_of_genera} genera associated with this query genome\n")

    #get all the keys for from a dictionary of accessions and genus names
    keys = [k for k, v in accession_genus_dict.items() if v == unique_genera[0]]

    #print the keys
    ic(keys)

    #Do different things depending how many unique genera were found
    if len(unique_genera) == 1:
        print_ok("Only found 1 genus so will proceed with getting all genomes associated with that genus")
        keys = [k for k, v in accession_genus_dict.items() if v == unique_genera[0]]
        number_ok_keys =len(keys)
        print_ok(f"Number of known species in the genus is {number_ok_keys} \n ")
        # create a command string for blastdbcmd
        get_genomes_cmd = f"blastdbcmd -db {blastdb_path} -entry {','.join(keys)} -out {known_taxa_path} "
        #subprocess.run(get_genomes_cmd, shell=True, check=True)
        ic(get_genomes_cmd)
        res = subprocess.getoutput(get_genomes_cmd)

    elif len(unique_genera) >1:
        print_ok("Found multiple genera that this query phage might be similar to so will proceed with processing them all")
        list_of_genus_accessions =[]
        for i in unique_genera:
            keys = [k for k, v in accession_genus_dict.items() if v == i]
            number_of_keys = len(keys)
            #ic(keys)
            list_of_genus_accessions.extend(keys)
            print_ok(f"Number of known species in the genus {i} is {number_of_keys}")
        ic(list_of_genus_accessions)
        ic(len(list_of_genus_accessions))
        get_genomes_cmd = f"blastdbcmd -db {blastdb_path} -entry {','.join(list_of_genus_accessions)} -out {known_taxa_path}"
        res = subprocess.getoutput(get_genomes_cmd)

    #get smallest mash distance

    min_dist = top_50['distance'].min()

    if min_dist < 0.04:
        print_ok("Phage is likely NOT a new species, will run further analysis now to to confirm this \n ")
        top_df = top_50[top_50['distance'] == min_dist]
        ic(top_df)

    elif min_dist > 0.04 and min_dist < 0.1:
        print_ok("It is not clear if the phage is a new species or not. Will run further analysis now to confirm this...\n")
        top_df = top_50[top_50['distance'] < 0.1]
        ic(top_df)
        print(top_50.genus.value_counts())

    elif min_dist > 0.1 and min_dist < 0.2:
        print_ok("Phage is a new species. Will run further analysis now ....\n")
        top_df = top_50[top_50['distance'] < 0.1]
        ic(top_df)

    #######run poor mans viridic
    with open(viridic_in_path, 'w') as merged_file:
        for file in known_taxa_path, query:                
            SeqIO.write(SeqIO.parse(file, 'fasta'), merged_file, 'fasta')
            

    PMV = PoorMansViridic(viridic_in_path, nthreads=threads, verbose=verbose)
    df1, pmv_outfile = PMV.run()

    ic(df1)
    ic(pmv_outfile)
    ic(PMV.dfM)

    # heatmap and distances
    if args.Figure:
        print_ok("\nWill calculate and save heatmaps now")
        heatmap(PMV.dfM, heatmap_file, top_right_matrix)
    else:
        print_error("\n Skipping calculating heatmaps and saving them \n ")

    PMV.save_similarities(similarities_file)

    #merge the ICTV dataframe with the results of viridic
    #fill in missing with Not Defined yet
    merged_df = pd.merge(df1, taxa_df, left_on='genome', right_on='Genbank',how='left' ).fillna('Not Defined Yet')

    ic(merged_df.head())

    #write dataframe to file
    merged_df.to_csv(taxa_csv_output_path, sep='\t', index=False)

    #create a copy of this dataframe for later use
    copy_merged_df = merged_df.copy()

    merged_df = merged_df[~merged_df['genome'].str.contains('query_')].reset_index(drop=True)
    #Count the number genera
    #excluding query
    num_unique_viridic_genus_clusters = merged_df['genus_cluster'].nunique()
    num_unique_ICTV_genera = merged_df['Genus'].nunique()

    #including query
    total_num_viridic_genus_clusters = copy_merged_df['genus_cluster'].nunique()
    total_num_viridic_species_clusters = copy_merged_df['species_cluster'].nunique()

    print(f"""\n\nTotal number of VIRIDIC-algorithm genus clusters in the input including QUERY sequence was: {total_num_viridic_genus_clusters}
    Total number of VIRIDIC-algorithm species clusters including QUERY sequence was {total_num_viridic_species_clusters} """)

    print(f"""\n\nNumber of current ICTV defined genera was: {num_unique_ICTV_genera}
    Number of VIRIDIC-algorithm predicted genera (excluding query) was: {num_unique_viridic_genus_clusters} """)

    if num_unique_ICTV_genera == num_unique_viridic_genus_clusters:
        print(f"""\n\nCurrent ICTV and VIRIDIC-algorithm predictions are consistent for the data that was used to compare against""")

    print_ok(f"Number of unique VIRIDIC-algorithm clusters at default cutoff of 70% is: {num_unique_viridic_genus_clusters}")
    print_ok(f"""Number of current ICTV genera associated with the reference genomes is {num_unique_ICTV_genera}""")

    #unique_viridic_genus_clusters = merged_df['genus_cluster'].unique().tolist()
    #num_unique_ICTV_genera = merged_df['Genus'].unique().tolist()

    species_genus_dict = merged_df.set_index('species_cluster')['Species'].to_dict()
    ic(species_genus_dict)
    #get information on the query from the dataframe
    #get species and genus cluster number
    query_row = copy_merged_df[copy_merged_df['genome'].str.contains('query_')]

    query_genus_cluster_number = query_row['genus_cluster'].values[0]
    query_species_cluster_number = query_row['species_cluster'].values[0]

    print(f"Cluster number of species is {query_species_cluster_number} and cluster of genus is {query_genus_cluster_number}")
    print(f"Genus cluster number is {query_genus_cluster_number}")

    #list of VIRIDIC genus and species numbers
    list_ICTV_genus_clusters = merged_df['genus_cluster'].unique().tolist()
    list_ICTV_species_clusters = merged_df['species_cluster'].unique().tolist()

    ic(list_ICTV_genus_clusters)
    ic(list_ICTV_species_clusters)

    #create a dictionary linking genus_cluster to genus data
    dict_genus_cluster_2_genus_name = merged_df.set_index('genus_cluster')['Genus'].to_dict()
    dict_species_cluster_2_species_name = merged_df.set_index('species_cluster')['Species'].to_dict()
    ic(dict_genus_cluster_2_genus_name)

    #check query is within a current genus. If not, then new Genus
    if query_genus_cluster_number not in dict_genus_cluster_2_genus_name:
        print_warn(f"""
        Cluster Number: {query_genus_cluster_number} is not in the dictionary of known Genera: {dict_genus_cluster_2_genus_name}""")
        print_res("""
        Phage is NOT within a current genus or species and therefore a both 
        a new Genus and species.""")
        sys.exit()

    predicted_genus_name = dict_genus_cluster_2_genus_name[query_genus_cluster_number]

    print(f"\nPredicted genus is: {predicted_genus_name}\n")
    #create a dict of species to species_cluster

    #if number of ICTV genera and predicted VIRIDIC genera match:

    if num_unique_ICTV_genera == num_unique_viridic_genus_clusters:
        print("""Current ICTV taxonomy and VIRIDIC-algorithm output appear to be consistent at the genus level""")

        #GENUS CHECK FIRST- Current genus and current species
        if query_genus_cluster_number in list_ICTV_genus_clusters and query_species_cluster_number in list_ICTV_species_clusters:
            print("""\nPhage is within a current genus and same as a current species 
             ....working out which one now .....\n""")
            predicted_genus = dict_genus_cluster_2_genus_name[query_genus_cluster_number]
            predicted_species = dict_species_cluster_2_species_name[query_species_cluster_number]
            print(f"""QUERY is in the genus: {predicted_genus} and is species: {predicted_species}""")
            #identify the row in the pandas data frame that is the same species
            matching_species_row = merged_df[merged_df['Species'] == predicted_species]
            ic(matching_species_row)
            list_of_S_data = matching_species_row.iloc[0].to_dict()
            ic(list_of_S_data)
            print_res(f"""\nQuery sequence is: 
                    Class: {list_of_S_data["Class"]}
                    Family: {list_of_S_data["Family"]}
                    Subfamily: {list_of_S_data["Subfamily"]}
                    Genus: {list_of_S_data["Genus"]}
                    Species: {list_of_S_data["Species"]}
                     """)
            with open(summary_output_path,'a') as file:
                file.write(f"""statement_current_genus_sp 
                           Class: {list_of_S_data["Class"]}\tFamily: {list_of_S_data["Family"]}\tSubfamily: {list_of_S_data["Subfamily"]}\tGenus: {list_of_S_data["Genus"]}\tSpecies: {list_of_S_data["Species"]}
                \n{summary_statement1}""")

            mash_df.to_csv(summary_output_path, mode='a', header=True, index=False,sep='\t')

            #WRITE CODE FOR GIVING INFO ON SPECIES

        #SAME GENUS but different species
        elif query_genus_cluster_number in list_ICTV_genus_clusters and query_species_cluster_number not in list_ICTV_species_clusters:
            print("""\nPhage is within a current genus, BUT is representative of a new species 
                     ....working out which one now .....\n""")

            matching_genus_rows = merged_df[merged_df['genus_cluster'] == query_genus_cluster_number]
            dict_exemplar_genus = matching_genus_rows.iloc[0].to_dict()
            genus_value = dict_exemplar_genus['Genus']
            ic(matching_genus_rows)
            ic(genus_value)

            print_res(f"""\nQuery sequence is: 
                    Class: {dict_exemplar_genus['Class']}
                    Family: {dict_exemplar_genus['Family']} 
                    Subfamily: {dict_exemplar_genus['Subfamily']}
                    Genus: {dict_exemplar_genus['Genus']}
                    Species: {dict_exemplar_genus['Genus']} new_name
             """)

            with open(summary_output_path, 'a') as file:
                file.write(f""" {statement_current_genus_new_sp}
    Class: {dict_exemplar_genus['Class']}\tFamily: {dict_exemplar_genus['Family']}\tSubfamily: {dict_exemplar_genus['Subfamily']}\tGenus: {dict_exemplar_genus['Genus']}\tSpecies: new_specices_name
    {summary_statement1}""")
            mash_df.to_csv(summary_output_path, mode='a', header=True, index=False,sep='\t')

        elif query_genus_cluster_number in list_ICTV_genus_clusters and query_species_cluster_number not in list_ICTV_species_clusters:
            print("""\nQuery does not fall within  a  current genus or species as defined by ICTV
            Therefore the query sequence is likely the first representative of both a new species and new genus
            Data produced by taxmyPHAGE will help you write a Taxonomy proposal so it can be offically classified
            WARNING taxmyPHAGE does not compare against all other known phages, only those that have been classified
            \n""")

            with open(summary_output_path, 'a') as file:
                file.write("""
            Query sequence can not be classified within a current genus or species, it is in:\n
            Remember taxmyPHAGE compared against viruses classified by the ICTV. Allowing determine if it represents a new 
            species or geneus. It does not tell you if it is similar to other phages that have yet to be classified
            You can do this by comparison with INPHARED database if you wish""")
            mash_df.to_csv(summary_output_path, mode='a', header=True, index=False,sep='\t')

    ######if number of VIRIDIC genera is greater than ICTV genera
    elif num_unique_ICTV_genera < num_unique_viridic_genus_clusters:
        print_error(f"""{summary_statement_inconsitent}\n""")

        if query_genus_cluster_number in list_ICTV_genus_clusters and query_species_cluster_number in list_ICTV_species_clusters:
            print_ok("""\nPhage is within a current genus and same as a current species 
             ....working out which one now .....\n""")
            if query_genus_cluster_number in list_ICTV_genus_clusters and query_species_cluster_number in list_ICTV_species_clusters:
                print("""\nPhage is within a current genus and same as a current species 
                 ....working out which one now .....\n""")
                predicted_genus = dict_genus_cluster_2_genus_name[query_genus_cluster_number]
                predicted_species = dict_species_cluster_2_species_name[query_species_cluster_number]
                print(f"""QUERY is in the genus:{predicted_genus} and is species: {predicted_species}""")
                # identify the row in the pandas data frame that is the same species
                matching_species_row = merged_df[merged_df['Species'] == predicted_species]
                ic(matching_species_row)
                list_of_S_data = matching_species_row.iloc[0].to_dict()
                ic(list_of_S_data)
                print_res(f"""\nQuery sequence is: 
                        Class: {list_of_S_data["Class"]}
                        Family: {list_of_S_data["Family"]}
                        Subfamily: {list_of_S_data["Subfamily"]}
                        Genus: {list_of_S_data["Genus"]}
                        Species: {list_of_S_data["Species"]}
                         """)
                with open(summary_output_path, 'a') as file:
                    file.write(f"""statement_current_genus_sp 
                               Class: {list_of_S_data["Class"]}\tFamily: {list_of_S_data["Family"]}\tSubfamily: {list_of_S_data["Subfamily"]}\tGenus:{list_of_S_data["Genus"]}\tSpecies: {list_of_S_data["Species"]}
                    \n{summary_statement1}""")

                mash_df.to_csv(summary_output_path, mode='a', header=True, index=False, sep='\t')

        elif query_genus_cluster_number in list_ICTV_genus_clusters and query_species_cluster_number not in list_ICTV_species_clusters:
            print_ok("""\nPhage is within a current genus, BUT is representative of a new species 
                     ....working out which one now .....\n""")

            matching_genus_rows = merged_df[merged_df['genus_cluster'] == query_genus_cluster_number]
            dict_exemplar_genus = matching_genus_rows.iloc[0].to_dict()
            genus_value =dict_exemplar_genus['Genus']
            ic(matching_genus_rows)
            ic(genus_value)

            print_res(f"""\nQuery sequence is in the;
                    Class: {dict_exemplar_genus['Class']}
                    Family: {dict_exemplar_genus['Family']} 
                    Subfamily: {dict_exemplar_genus['Subfamily']}
                    Genus: {dict_exemplar_genus['Genus']}
                    Species: new_species_name
             """)

            with open(summary_output_path, 'a') as file:
                file.write(f"""\n
    Query sequence can be classified within a current genus and represents a new species, it is in:\n
    Class: {dict_exemplar_genus['Class']}\tFamily: {dict_exemplar_genus['Family']}\tSubfamily: {dict_exemplar_genus['Subfamily']}\tGenus: {dict_exemplar_genus['Genus']}\tSpecies: name_your_species
    \n 
    {summary_statement1}
    """)
            mash_df.to_csv(summary_output_path, mode='a', header=True, index=False,sep='\t')

    run_time = str(timedelta(seconds = time.time() - timer_start))
    print(f"Run time for {fasta_file}: {run_time}", file=sys.stderr)
