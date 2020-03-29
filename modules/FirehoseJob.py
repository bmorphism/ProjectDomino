###

from collections import deque, defaultdict
import cudf, datetime, gc, os, string, sys, time, uuid
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import simplejson as json #nan serialization
from twarc import Twarc

from .Timer import Timer
from .TwarcPool import TwarcPool
from .Neo4jDataAccess import Neo4jDataAccess

#############################

###################

object_dtype = pd.Series([ [1,2], ['3', '4'] ]).dtype
string_dtype = 'object'
id_type = np.int64

### If col missing in df, create with below dtype & default val
EXPECTED_COLS = [
    ('contributors', 'object', []),
    ('coordinates', 'object', None),
    ('created_at', 'object', None), #FIXME datetime[s]
    ('display_text_range', 'object', None),
    ('extended_entities', 'object', None),
    ('entities', 'object', None),
    ('favorited', np.bool, None),
    ('followers', 'object', None),
    ('favorite_count', np.int64, 0),
    ('full_text', 'object', None),
    ('geo', 'object', None),
    ('id', np.int64, None),
    ('id_str', string_dtype, None),
    ('in_reply_to_screen_name', string_dtype, None),
    ('in_reply_to_status_id', np.int64, None),
    ('in_reply_to_status_id_str', string_dtype, None),
    ('in_reply_to_user_id', np.int64, None),
    ('in_reply_to_user_id_str', string_dtype, None),
    ('is_quote_status', np.bool, None),
    ('lang', string_dtype, None),
    ('place', string_dtype, None),    
    ('possibly_sensitive', np.bool_, False),
    ('quoted_status', string_dtype, 0.0),
    ('quoted_status_id', id_type, 0),
    ('quoted_status_id_str', string_dtype, None),
    ('quoted_status_permalink', string_dtype, None),
    ('in_reply_to_status_id', id_type, 0),
    ('in_reply_to_user_id', id_type, 0),
    ('retweet_count', np.int64, 0),
    ('retweeted', np.bool, None),
    ('retweeted_status', string_dtype, None),
    ('scopes','object', None),
    ('source', string_dtype, None),
    ('truncated', np.bool, None),
    ('user', string_dtype, None),
    ('withheld_in_countries', object_dtype, [])
]

DROP_COLS = [ 'withheld_in_countries' ]


#### PARQUET WRITER BARFS
#sizes_t = pa.struct({
#                'h': pa.int64(),
#                'resize': pa.string(),
#                'w': pa.int64()
#            })
#
#extended_entities_t = pa.struct({
#    'media': pa.list_(pa.struct({
#        'display_url': pa.string(),
#        'expanded_url': pa.string(),
#        'ext_alt_text': pa.string(),
#        'id': pa.int64(),
#        'id_str': pa.string(),
#        'indices': pa.list_(pa.int64()),
#        'media_url': pa.string(),
#        'media_url_https': pa.string(),
#        'sizes': pa.struct({
#            'large': sizes_t,
#            'medium': sizes_t,
#            'small': sizes_t,
#            'thumb': sizes_t
#        }),
#        'source_status_id': pa.int64(),
#        'source_status_id_str': pa.string(),
#        'source_user_id': pa.int64(),
#        'source_user_id_str': pa.string(),
#        'type': pa.string(),
#        'url': pa.string()
#    }))})

### When dtype -> arrow ambiguious, override
KNOWN_FIELDS = [    
    #[0, 'contributors', ??],
    [1, 'coordinates', pa.string()],
    [2, 'created_at', pa.string()],
    [3, 'display_text_range', pa.list_(pa.int64())],
    [4, 'entities', pa.string()],
    [5, 'extended_entities', pa.string()], #extended_entities_t ],
    [7, 'favorited', pa.bool_()],
    [9, 'full_text', pa.string()],
    [10, 'geo', pa.string()],
    [11, 'id', pa.int64() ],
    [12, 'id_str', pa.string() ],
    [13, 'in_reply_to_screen_name', pa.string() ],
    [14, 'in_reply_to_status_id', pa.int64() ],
    [15, 'in_reply_to_status_id_str', pa.string() ],
    [16, 'in_reply_to_user_id', pa.int64() ],
    [17, 'in_reply_to_user_id_str', pa.string() ],
    [18, 'is_quote_status', pa.bool_() ],
    [19, 'lang', pa.string() ],
   # [20, 'place', pa.string()],
   # [21, 'possibly_sensitive', pa.bool_()],
    #[22, 'quoted_status', pa.string()],
    #[23, 'quoted_status_id', pa.int64()],
    #[24, 'quoted_status_id_str', pa.string()],
    #[25, 'quoted_status_permalink', pa.string()],
   # [26, 'retweet_count', pa.int64()],
   # [27, 'retweeted', pa.bool_()],
    #[28, 'retweeted_status', pa.string()],
   # [29, 'scopes', pa.string()],  # pa.struct({'followers': pa.bool_()})],
    #[30, 'source', pa.string()],
   # [31, 'truncated', pa.bool_()],
   # [32, 'user', pa.string()],
    #[33, 'withheld_in_countries', pa.list_(pa.string())],
]

#############################


class FirehoseJob:
    
    ###################

    MACHINE_IDS = (375, 382, 361, 372, 364, 381, 376, 365, 363, 362, 350, 325, 335, 333, 342, 326, 327, 336, 347, 332)
    SNOWFLAKE_EPOCH = 1288834974657
    
    EXPECTED_COLS = EXPECTED_COLS
    KNOWN_FIELDS = KNOWN_FIELDS
    DROP_COLS = DROP_COLS
    
    
    def __init__(self, creds = [], TWEETS_PER_PROCESS=100, TWEETS_PER_ROWGROUP=5000, save_to_neo=False, PARQUET_SAMPLE_RATE_TIME_S=None):
        self.queue = deque()
        self.writers = {
            'snappy': None
            #,'vanilla': None
        }
        self.last_write_epoch = ''
        self.current_table = None
        self.schema = None
        self.timer = Timer()
        
        self.twarc_pool = TwarcPool([ 
            Twarc(o['consumer_key'], o['consumer_secret'], o['access_token'], o['access_token_secret'])
            for o in creds 
        ])
        self.save_to_neo=save_to_neo
        self.TWEETS_PER_PROCESS = TWEETS_PER_PROCESS #100
        self.TWEETS_PER_ROWGROUP = TWEETS_PER_ROWGROUP #100 1KB x 1000 = 1MB uncompressed parquet
        self.PARQUET_SAMPLE_RATE_TIME_S = PARQUET_SAMPLE_RATE_TIME_S        
        self.last_df = None
        self.last_arr = None
        self.last_write_arr = None
        self.last_writes_arr = []

        self.needs_to_flush = False

        self.__file_names = []
        
    def __del__(self):
        print('__del__')
        self.destroy()        
        
    def destroy(self, job_name='generic_job'):
        print('flush before destroying..')
        self.flush(job_name)
        print('destroy', self.writers.keys())        
        for k in self.writers.keys():
            if not (self.writers[k] is None):
                print('Closing parquet writer %s' % k)
                writer = self.writers[k]
                writer.close()
                print('... sleep 1s...')
                time.sleep(1)
                self.writers[k] = None
                print('... sleep 1s...')
                time.sleep(1)
                print('... Safely closed %s' % k)
            else:
                print('Nothing to close for writer %s' % k)
                
    ###################

    def get_creation_time(self, id):
        return ((id >> 22) + 1288834974657)

    def machine_id(self, id):
        return (id >> 12) & 0b1111111111

    def sequence_id(self, id):
        return id & 0b111111111111
    
    
    ###################
    
    valid_file_name_chars = frozenset("-_%s%s" % (string.ascii_letters, string.digits))
    
    def clean_file_name(self, filename):
        return ''.join(c for c in filename if c in FirehoseJob.valid_file_name_chars)

    #clean series before reaches arrow
    def clean_series(self, series):        
        try:
            identity = lambda x: x
            
            series_to_json_string = (lambda series: series.apply(lambda x: json.dumps(x, ignore_nan=True)))
            
            ##objects: put here to skip str coercion
            coercions = {
                'display_text_range': identity,
                'contributors': identity,
                'created_at': lambda series: series.values.astype('unicode'),
                'possibly_sensitive': (lambda series: series.fillna(False)),
                'quoted_status_id': (lambda series: series.fillna(0).astype('int64')),
                'extended_entities': series_to_json_string,
                'in_reply_to_status_id': (lambda series: series.fillna(0).astype('int64')),
                'in_reply_to_user_id': (lambda series: series.fillna(0).astype('int64')),
                'scopes': series_to_json_string,
                'followers': identity, #(lambda series: pd.Series([str(x) for x in series.tolist()]).values.astype('unicode')),
                'withheld_in_countries': identity,
            }
            if series.name in coercions.keys():
                return coercions[series.name](series)
            elif series.dtype.name == 'object':
                return series.values.astype('unicode')
            else:
                return series
        except Exception as exn:
            print('coerce exn on col', series.name, series.dtype)
            print('first', series[:1])
            print(exn)
            return series

    #clean df before reaches arrow
    def clean_df(self, raw_df):
        self.timer.tic('clean', 1000)
        try:
            new_cols = {
                c: pd.Series([c_default] * len(raw_df), dtype=c_dtype) 
                for (c, c_dtype, c_default) in FirehoseJob.EXPECTED_COLS 
                if not c in raw_df
            }
            all_cols_df = raw_df.assign(**new_cols)
            sorted_df = all_cols_df.reindex(sorted(all_cols_df.columns), axis=1)
            return pd.DataFrame({c: self.clean_series(sorted_df[c]) for c in sorted_df.columns})
        except Exception as exn:
            print('failed clean')
            print(exn)
            raise exn
        finally:
            self.timer.toc('clean')
        
    
    def folder_last(self):
        return self.__folder_last
    
    def files(self):
        return self.__file_names.copy()
        

    #TODO <topic>/<year>/<mo>/<day>/<24hour_utc>_<nth>.parquet (don't clobber..)
    def pq_writer(self, table, job_name='generic_job'):
        try:
            self.timer.tic('write', 1000)
            
            job_name = self.clean_file_name(job_name)
            
            folder = "firehose_data/%s" % job_name            
            print('make folder if not exists: %s' % folder)            
            os.makedirs(folder, exist_ok=True)
            self.__folder_last = folder
            
            vanilla_file_suffix = 'vanilla2.parquet'
            snappy_file_suffix = 'snappy2.parquet'            
            time_prefix = datetime.datetime.now().strftime("%Y_%m_%d_%H")
            run = 0
            file_prefix = ""
            while (file_prefix == "") \
                or os.path.exists(file_prefix + vanilla_file_suffix) \
                or os.path.exists(file_prefix + snappy_file_suffix):
                run = run + 1
                file_prefix = "%s/%s_b%s." % ( folder, time_prefix, run )
            if run > 1:
                print('Starting new batch for existing hour')
            vanilla_file_name = file_prefix + vanilla_file_suffix
            snappy_file_name = file_prefix + snappy_file_suffix
            
            #########################################################
            
                          
            #########################################################
            if ('vanilla' in self.writers) and ( (self.writers['vanilla'] is None) or self.last_write_epoch != file_prefix ):
                print('Creating vanilla writer', vanilla_file_name)
                try:
                    #first write
                    #os.remove(vanilla_file_name)
                    1
                except Exception as exn:
                    print('Could not rm vanilla parquet', exn)
                self.writers['vanilla'] = pq.ParquetWriter(
                    vanilla_file_name, 
                    schema=table.schema,
                compression='NONE')
                self.__file_names.append(vanilla_file_name)
                
            if ('snappy' in self.writers) and ( (self.writers['snappy'] is None) or self.last_write_epoch != file_prefix ):
                print('Creating snappy writer', snappy_file_name)
                try:
                    #os.remove(snappy_file_name)
                    1
                except Exception as exn:
                    print('Could not rm snappy parquet', exn)                
                self.writers['snappy'] = pq.ParquetWriter(
                    snappy_file_name, 
                    schema=table.schema,
                    compression={
                        field.name.encode(): 'SNAPPY'
                        for field in table.schema
                    })
                self.__file_names.append(snappy_file_name)
                
            self.last_write_epoch = file_prefix
            ######################################################
                
            for name in self.writers.keys():
                try:
                    print('Writing %s (%s x %s)' % (
                        name, table.num_rows, table.num_columns))
                    self.timer.tic('writing_%s' % name, 20, 1)
                    writer = self.writers[name]
                    writer.write_table(table)
                    self.timer.toc('writing_%s' % name, table.num_rows)
                    #########
                    print('######## TRANSACTING')
                    self.last_write_arr = table
                    self.last_writes_arr.append(table)
                    #########
                except Exception as exn:
                    print('... failed to write to parquet')
                    print(exn)
            print('######### ALL WRITTEN #######')

        finally:
            self.timer.toc('write')
            
    def flush(self, job_name="generic_job"):
        try:
            if self.current_table is None or self.current_table.num_rows == 0:
                return
            print('writing to parquet then clearing current_table..')
            self.pq_writer(self.current_table, job_name)
            if self.save_to_neo:
                print('Writing to Neo4j')
                Neo4jDataAccess(True).save_parquet_df_to_graph(self.current_table.to_pandas(), job_name)
        finally:
            print('flush clearing self.current_table')
            self.current_table = None

            
    def tweets_to_df(self, tweets):
        try:
            self.timer.tic('to_pandas', 1000)
            df = pd.DataFrame(tweets)
            df = df.drop(columns=FirehoseJob.DROP_COLS, errors='ignore')
            self.last_df = df
            return df
        except Exception as exn:
            print('Failed tweets->pandas')
            print(exn)
            raise exn
        finally:
            self.timer.toc('to_pandas')
    
    def df_with_schema_to_arrow(self, df, schema):
        try:
            self.timer.tic('df_with_schema_to_arrow', 1000)
            table = None
            try:
                #if len(df['followers'].dropna()) > 0:
                #    print('followers!')
                #    print(df['followers'].dropna())
                #    raise Exception('ok')
                table = pa.Table.from_pandas(df, schema)
                if len(df.columns) != len(schema):
                    print('=========================')
                    print('DATA LOSS WARNING: df has cols not in schema, dropping') #reverse is an exn
                    for col_name in df.columns:
                        hits = [field for field in schema if field.name==col_name]
                        if len(hits) == 0:
                            print('-------')
                            print('arrow schema missing col %s ' % col_name)
                            print('df dtype',df[col_name].dtype)
                            print(df[col_name].dropna())
                            print('-------')
            except Exception as exn:
                print('============================')
                print('failed nth arrow from_pandas')
                print('-------')
                print(exn)
                print('-------')
                try:
                    print('followers', df['followers'].dropna())
                    print('--------')
                    print('coordinates', df['coordinates'].dropna())
                    print('--------')
                    print('dtypes', df.dtypes)
                    print('--------')
                    print(df.sample(min(5, len(df))))
                    print('--------')
                    print('arrow')
                    print([schema[k] for k in range(0, len(schema))])
                    print('~~~~~~~~')
                    if not (self.current_table is None):
                        try:
                            print(self.current_table.to_pandas()[:3])
                            print('----')
                            print([self.current_table.schema[k] for k in range(0, self.current_table.num_columns)])
                        except Exception as exn2:
                            print('cannot to_pandas print..', exn2)
                except:
                    1
                print('-------')
                err_file_name = 'fail_' + str(uuid.uuid1())
                print('Log failed batch and try to continue! %s' % err_file_name)
                df.to_csv('./' + err_file_name)
                raise exn
            for i in range(len(schema)):
                if not (schema[i].equals(table.schema[i])):
                    print('EXN: Schema mismatch on col # %s', i)
                    print(schema[i])
                    print('-----')
                    print(table.schema[i])
                    print('-----')
                    raise Exception('mismatch on col # ' % i)
            return table
        finally:
            self.timer.toc('df_with_schema_to_arrow')
            
    
    def concat_tables(self, table_old, table_new):
        try:
            self.timer.tic('concat_tables', 1000)
            return pa.concat_tables([table_old, table_new]) #promote..
        except Exception as exn:
            print('=========================')
            print('Error combining arrow tables, likely new table mismatches old')
            print('------- cmp')
            for i in range(0, table_old.num_columns):
                if i >= table_new.num_columns:
                    print('new table does not have enough columns to handle %i' % i)
                elif table_old.schema[i].name != table_new.schema[i].name:
                    print('ith col name mismatch', i, 
                          'old', (table_old.schema[i]), 'vs new', table_new.schema[i])
            print('------- exn')
            print(exn)
            print('-------')
            raise exn
        finally:
            self.timer.toc('concat_tables')
            
                
    def df_to_arrow(self, df):
        
        print('first process..')
        table = pa.Table.from_pandas(df)

        print('patching lossy dtypes...')        
        schema = table.schema
        for [i, name, field_t] in FirehoseJob.KNOWN_FIELDS:
            if schema[i].name != name:
                raise Exception('Mismatched index %s: %s -> %s' % (
                    i, schema[i].name, name
                ))
            schema = schema.set(i, pa.field(name, field_t))
            
        print('re-coercing..')
        table_2 = pa.Table.from_pandas(df, schema)
        #if not table_2.schema.equals(schema):
        for i in range(0, len(schema)):
            if not (schema[i].equals(table_2.schema[i])):
                print('=======================')
                print('EXN: schema mismatch %s' % i)
                print(schema[i])
                print('-----')
                print(table_2.schema[i])
                print('-----')
                raise Exception('schema mismatch %s' % i)
        print('Success!')
        print('=========')
        print('First tweets arrow schema')
        for i in range(0, len(table_2.schema)):
            print(i, table_2.schema[i])
        print('////////////')
        return table_2
    
    def process_tweets_notify_hydrating(self):
        if not (self.current_table is None):
            self.timer.toc('tweet', self.current_table.num_rows)
        self.timer.tic('tweet', 40, 40)
                
        self.timer.tic('hydrate', 40, 40)

    
    # Call process_tweets_notify_hydrating() before
    def process_tweets(self, tweets, job_name='generic_job'):
        
        self.timer.toc('hydrate')
        
        self.timer.tic('overall_compute', 40, 40)

        raw_df = self.tweets_to_df(tweets)
        df = self.clean_df(raw_df)

        table = None
        if self.schema is None:
            table = self.df_to_arrow(df)
            self.schema = table.schema
        else:
            try:
                table = self.df_with_schema_to_arrow(df, self.schema)
            except:
                print('conversion failed, skipping batch...')
                self.timer.toc('overall_compute')
                return
            
        self.last_arr = table

        if self.current_table is None:
            self.current_table = table
        else:
            self.current_table = self.concat_tables(self.current_table, table)

        out = self.current_table #or just table (without intermediate concats since last flush?)
            
        if not (self.current_table is None) \
            and ((self.current_table.num_rows > self.TWEETS_PER_ROWGROUP) or self.needs_to_flush):
            self.flush(job_name)
            self.needs_to_flush = False
        else:
            1
            #print('skipping, has table ? %s, num rows %s' % ( 
            #    not (self.current_table is None), 
            #    0 if self.current_table is None else self.current_table.num_rows))

        self.timer.toc('overall_compute')
        
        return out
        
    def process_tweets_generator(self, tweets_generator, job_name='generic_job'):
       
        def flusher(tweets_batch):
            try:
                self.needs_to_flush = True
                return self.process_tweets(tweets_batch, job_name)
            except:
                print('failed processing batch, continuing...')
    
        tweets_batch = []
        last_flush_time_s = time.time()
        
        try:
            for tweet in tweets_generator:
                tweets_batch.append(tweet)
                
                if len(tweets_batch) > self.TWEETS_PER_PROCESS:
                    self.needs_to_flush = True
                elif not (self.PARQUET_SAMPLE_RATE_TIME_S is None) \
                        and time.time() - last_flush_time_s >= self.PARQUET_SAMPLE_RATE_TIME_S:                    
                    self.needs_to_flush = True
                    last_flush_time_s = time.time()
                
                if self.needs_to_flush:
                    try:
                        yield flusher(tweets_batch)
                    except:
                        print('Write fail, continuing..')
                    finally:
                        tweets_batch = []
            print('===== PROCESSED ALL GENERATOR TASKS, FINISHING ====')
            yield flusher(tweets_batch)
            print('/// FLUSHED, DONE')
        except KeyboardInterrupt as e:
            print('========== FLUSH IF SLEEP INTERRUPTED')
            self.destroy()
            del fh
            gc.collect()
            print('explicit GC...')
            print('Safely exited!')
            
        
    ################################################################################

    def process_ids(self, ids_to_process, job_name=None):

        self.process_tweets_notify_hydrating()
        
        if job_name is None:
            job_name = "process_ids_%s" % (ids_to_process[0] if len(ids_to_process) > 0 else "none")

        tweets = ( tweet for tweet in self.twarc_pool.next_twarc().hydrate(ids_to_process) )
        
        for arr in self.process_tweets_generator(tweets, job_name):
            yield arr
        
    def process_id_file(self, path, job_name=None):
        
        pdf = cudf.read_csv(path, header=None).to_pandas()
        lst = pdf['0'].to_list()
        if job_name is None:
            job_name = "id_file_%s" % path
        print('loaded %s ids, hydrating..' % len(lst))
            
        for arr in self.process_ids(lst, job_name):
            yield arr
        
        
    def search(self,input="", job_name=None):        
        
        self.process_tweets_notify_hydrating()
        
        if job_name is None:
            job_name = "search_%s" % input[:20]
        
        tweets = (tweet for tweet in self.twarc_pool.next_twarc().search(input))

        self.process_tweets_generator(tweets, job_name)
        
        
    def search_stream_by_keyword(self,input="", job_name=None):

        self.process_tweets_notify_hydrating()
        
        if job_name is None:
            job_name = "search_stream_by_keyword_%s" % input[:20]

        tweets = [tweet for tweet in self.twarc_pool.next_twarc().filter(track=input)]
        
        self.process_tweets(tweets, job_name)


    def search_by_location(self,input="", job_name=None):
        
        self.process_tweets_notify_hydrating()

        if job_name is None:
            job_name = "search_by_location_%s" % input[:20]

        tweets = [tweet for tweet in self.twarc_pool.next_twarc().filter(locations=input)]
        
        self.process_tweets(tweets, job_name)
        

    # 
    def user_timeline(self,input=[""], job_name=None, **kwargs):
        if not (type(input) == list):
            input = [ input ]
        try:
            self.process_tweets_notify_hydrating()

            if job_name is None:
                job_name = "user_timeline_%s_%s" % ( len(input), '_'.join(input) )

            for user in input:
                print('starting user %s' % user)
                tweet_count = 0
                for tweet in self.twarc_pool.next_twarc().timeline(screen_name=user, **kwargs):                    
                    #print('got user', user, 'tweet', str(tweet)[:50])
                    self.process_tweets([tweet], job_name)
                    tweet_count = tweet_count + 1
                print('    ... %s tweets' % tweet_count)
                
            self.destroy()
        except KeyboardInterrupt as e:
            print('Flushing..')
            self.destroy(job_name)
            print('Explicit GC')
            gc.collect()
            print('Safely exited!')


    def ingest_range(self, begin, end, job_name=None):  # This method is where the magic happens

        if job_name is None:
            job_name = "ingest_range_%s_to_%s" % (begin, end)
            
        for epoch in range(begin, end):  # Move through each millisecond
            time_component = (epoch - FirehoseJob.SNOWFLAKE_EPOCH) << 22
            for machine_id in FirehoseJob.MACHINE_IDS:  # Iterate over machine ids
                for sequence_id in [0]:  # Add more sequence ids as needed
                    twitter_id = time_component + (machine_id << 12) + sequence_id
                    self.queue.append(twitter_id)
                    if len(self.queue) >= self.TWEETS_PER_PROCESS:
                        ids_to_process = []
                        for i in range(0, self.TWEETS_PER_PROCESS):
                            ids_to_process.append(self.queue.popleft())
                        self.process_ids(ids_to_process, job_name)

